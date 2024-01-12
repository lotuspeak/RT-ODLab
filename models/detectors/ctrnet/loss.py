import torch
import torch.nn.functional as F

from utils.box_ops import get_ious
from utils.distributed_utils import get_world_size, is_dist_avail_and_initialized

from .matcher import AlignedSimOTA


class Criterion(object):
    def __init__(self, args, cfg, device, num_classes=80):
        self.args = args
        self.cfg = cfg
        self.device = device
        self.num_classes = num_classes
        self.max_epoch = args.max_epoch
        self.no_aug_epoch = args.no_aug_epoch
        self.aux_bbox_loss = False
        # --------------- Loss config ---------------
        self.loss_cls_weight = cfg['loss_cls_weight']
        self.loss_box_weight = cfg['loss_box_weight']
        # --------------- Matcher config ---------------
        self.matcher_hpy = cfg['matcher_hpy']['main']
        self.matcher = AlignedSimOTA(soft_center_radius = self.matcher_hpy['soft_center_radius'],
                                     topk_candidates    = self.matcher_hpy['topk_candidates'],
                                     num_classes        = num_classes,
                                     )
        # --------------- Aux Matcher config ---------------
        self.aux_matcher_hpy = cfg['matcher_hpy']['aux']
        self.aux_matcher = AlignedSimOTA(soft_center_radius = self.aux_matcher_hpy['soft_center_radius'],
                                         topk_candidates    = self.aux_matcher_hpy['topk_candidates'],
                                         num_classes        = num_classes,
                                         )

    # -------------------- Basic loss functions --------------------
    def loss_classes(self, pred_cls, target, beta=2.0):
        # Quality FocalLoss
        """
            pred_cls: (torch.Tensor): [N, C]。
            target:   (tuple([torch.Tensor], [torch.Tensor])): label -> (N,), score -> (N)
        """
        label, score = target
        pred_sigmoid = pred_cls.sigmoid()
        scale_factor = pred_sigmoid
        zerolabel = scale_factor.new_zeros(pred_cls.shape)

        ce_loss = F.binary_cross_entropy_with_logits(
            pred_cls, zerolabel, reduction='none') * scale_factor.pow(beta)
        
        bg_class_ind = pred_cls.shape[-1]
        pos = ((label >= 0) & (label < bg_class_ind)).nonzero().squeeze(1)
        pos_label = label[pos].long()

        scale_factor = score[pos] - pred_sigmoid[pos, pos_label]

        ce_loss[pos, pos_label] = F.binary_cross_entropy_with_logits(
            pred_cls[pos, pos_label], score[pos],
            reduction='none') * scale_factor.abs().pow(beta)

        return ce_loss
    
    def loss_bboxes(self, pred_box, gt_box):
        ious = get_ious(pred_box, gt_box, box_mode="xyxy", iou_type='giou')
        loss_box = 1.0 - ious

        return loss_box
    
    def loss_bboxes_aux(self, pred_reg, gt_box, anchors, stride_tensors):
        # xyxy -> cxcy&bwbh
        gt_cxcy = (gt_box[..., :2] + gt_box[..., 2:]) * 0.5
        gt_bwbh = gt_box[..., 2:] - gt_box[..., :2]
        # encode gt box
        gt_cxcy_encode = (gt_cxcy - anchors) / stride_tensors
        gt_bwbh_encode = torch.log(gt_bwbh / stride_tensors)
        gt_box_encode = torch.cat([gt_cxcy_encode, gt_bwbh_encode], dim=-1)
        # l1 loss
        loss_box_aux = F.l1_loss(pred_reg, gt_box_encode, reduction='none')

        return loss_box_aux


    # -------------------- Task loss functions --------------------
    def compute_loss(self, outputs, targets, aux_loss=False, epoch=0):
        """
            Input:
                outputs: (Dict) -> {
                    'pred_cls': (List[torch.Tensor] -> [B, M, Nc]),
                    'pred_reg': (List[torch.Tensor] -> [B, M, 4]),
                    'pred_box': (List[torch.Tensor] -> [B, M, 4]),
                    'strides':  (List[Int])
                }
                target: (List[Dict]) [
                    {'boxes':  (torch.Tensor) -> [N, 4], 
                     'labels': (torch.Tensor) -> [N,],
                     ...}, ...
                     ]
            Output:
                loss_dict: (Dict) -> {
                    'loss_cls': (torch.Tensor) It is a scalar.),
                    'loss_box': (torch.Tensor) It is a scalar.),
                    'loss_box_aux': (torch.Tensor) It is a scalar.),
                    'losses':  (torch.Tensor) It is a scalar.),
                }
        """
        bs = outputs['pred_cls'].shape[0]
        device = outputs['pred_cls'].device
        stride = outputs['stride']
        anchors = outputs['anchors']
        # preds: [B, M, C]
        cls_preds = outputs['pred_cls']
        box_preds = outputs['pred_box']
        
        # --------------- label assignment ---------------
        cls_targets = []
        box_targets = []
        assign_metrics = []
        for batch_idx in range(bs):
            tgt_labels = targets[batch_idx]["labels"].to(device)  # [N,]
            tgt_bboxes = targets[batch_idx]["boxes"].to(device)   # [N, 4]
            if not aux_loss:
                assigned_result = self.matcher(stride=stride,
                                               anchors=anchors,
                                               pred_cls=cls_preds[batch_idx].detach(),
                                               pred_box=box_preds[batch_idx].detach(),
                                               gt_labels=tgt_labels,
                                               gt_bboxes=tgt_bboxes
                                               )
            else:
                assigned_result = self.aux_matcher(stride=stride,
                                                   anchors=anchors,
                                                   pred_cls=cls_preds[batch_idx].detach(),
                                                   pred_box=box_preds[batch_idx].detach(),
                                                   gt_labels=tgt_labels,
                                                   gt_bboxes=tgt_bboxes
                                                   )
            cls_targets.append(assigned_result['assigned_labels'])
            box_targets.append(assigned_result['assigned_bboxes'])
            assign_metrics.append(assigned_result['assign_metrics'])

        # List[B, M, C] -> Tensor[BM, C]
        cls_targets = torch.cat(cls_targets, dim=0)
        box_targets = torch.cat(box_targets, dim=0)
        assign_metrics = torch.cat(assign_metrics, dim=0)

        # FG cat_id: [0, num_classes -1], BG cat_id: num_classes
        bg_class_ind = self.num_classes
        pos_inds = ((cls_targets >= 0) & (cls_targets < bg_class_ind)).nonzero().squeeze(1)
        num_fgs = assign_metrics.sum()

        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_fgs)
        num_fgs = (num_fgs / get_world_size()).clamp(1.0).item()

        # ------------------ Classification loss ------------------
        cls_preds = cls_preds.view(-1, self.num_classes)
        loss_cls = self.loss_classes(cls_preds, (cls_targets, assign_metrics))
        loss_cls = loss_cls.sum() / num_fgs

        # ------------------ Regression loss ------------------
        box_preds_pos = box_preds.view(-1, 4)[pos_inds]
        box_targets_pos = box_targets[pos_inds]
        loss_box = self.loss_bboxes(box_preds_pos, box_targets_pos)
        loss_box = loss_box.sum() / num_fgs

        # total loss
        losses = self.loss_cls_weight * loss_cls + \
                 self.loss_box_weight * loss_box

        # ------------------ Aux regression loss ------------------
        loss_box_aux = None
        if epoch >= (self.max_epoch - self.no_aug_epoch - 1):
            ## reg_preds
            reg_preds = outputs['pred_reg']
            reg_preds_pos = reg_preds.view(-1, 4)[pos_inds]
            ## anchor tensors
            anchors_tensors = outputs['anchors'][None].repeat(bs, 1, 1)
            anchors_tensors_pos = anchors_tensors.view(-1, 2)[pos_inds]
            ## stride tensors
            stride_tensors = outputs['stride_tensors'][None].repeat(bs, 1, 1)
            stride_tensors_pos = stride_tensors.view(-1, 1)[pos_inds]
            ## aux loss
            loss_box_aux = self.loss_bboxes_aux(reg_preds_pos, box_targets_pos, anchors_tensors_pos, stride_tensors_pos)
            loss_box_aux = loss_box_aux.sum() / num_fgs

            losses += loss_box_aux

        # Loss dict
        if loss_box_aux is None:
            loss_dict = dict(
                    loss_cls = loss_cls,
                    loss_box = loss_box,
                    losses = losses
            )
        else:
            loss_dict = dict(
                    loss_cls = loss_cls,
                    loss_box = loss_box,
                    loss_box_aux = loss_box_aux,
                    losses = losses
                    )

        return loss_dict

    def __call__(self, outputs, targets, epoch=0):
        # -------------- Main loss --------------
        main_loss_dict = self.compute_loss(outputs, targets, epoch)
        
        # -------------- Aux loss --------------
        aux_loss_dict = self.compute_loss(outputs['aux_outputs'], targets, epoch)

        # Reformat loss dict
        loss_dict = dict()
        loss_dict['losses'] = main_loss_dict['losses'] + aux_loss_dict['losses']
        for k in main_loss_dict:
            if k != 'losses':
                loss_dict[k] = main_loss_dict[k]
        for k in aux_loss_dict:
            if k != 'losses':
                loss_dict[k+'_aux'] = aux_loss_dict[k]
        
        return loss_dict


def build_criterion(args, cfg, device, num_classes):
    criterion = Criterion(args, cfg, device, num_classes)

    return criterion


if __name__ == "__main__":
    pass