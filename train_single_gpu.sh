# -------------------------- Train YOLOX series --------------------------
python train.py \
        --cuda \
        -d coco \
        --root /mnt/share/ssd2/dataset/ \
        -m yolox_l \
        -bs 8 \
        -size 640 \
        --wp_epoch 3 \
        --max_epoch 300 \
        --eval_epoch 10 \
        --no_aug_epoch 15 \
        --grad_accumulate 8 \
        --ema \
        --fp16 \
        --multi_scale \
        # --load_cache \
        # --resume weights/coco/yolox_m/yolox_m_best.pth \
        # --eval_first

# -------------------------- Train YOLOv1~v5 & v7 series --------------------------
# python train.py \
#         --cuda \
#         -d coco \
#         --root /mnt/share/ssd2/dataset/ \
#         -m yolov5_l \
#         -bs 16 \
#         -size 640 \
#         --wp_epoch 3 \
#         --max_epoch 300 \
#         --eval_epoch 10 \
#         --no_aug_epoch 10 \
#         --ema \
#         --fp16 \
#         --multi_scale \
#         # --load_cache \
#         # --resume weights/coco/yolov5_l/yolov5_l_best.pth \
#         # --eval_first

# -------------------------- Train My RTCDet series --------------------------
# python train.py \
#         --cuda \
#         -d coco \
#         --root /mnt/share/ssd2/dataset/ \
#         -m rtcdet_v1_l \
#         -bs 16 \
#         -size 640 \
#         --wp_epoch 3 \
#         --max_epoch 300 \
#         --eval_epoch 10 \
#         --no_aug_epoch 20 \
#         --grad_accumulate 8 \
#         --ema \
#         --fp16 \
#         --multi_scale \
#         # --load_cache \
#         # --resume weights/coco/rtcdet_v1_l/rtcdet_v1_l_best.pth \
#         # --eval_first
