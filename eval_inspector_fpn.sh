export CUDA_VISIBLE_DEVICES=1
python inspector.py \
--evaluation \
--generate_image \
--dataset_name "DeepGlobe" \
--data_path "/vinai/chuonghm/deep_globe" \
--n_class 7 \
--backbone resnet_fpn \
--num_scaling_level 3 \
--log_path "/vinai/chuonghm/inspector/logs" \
--task_name "eval_inspector_deepglobe_global_resnetfpn" \
--restore_path "/vinai/chuonghm/inspector/logs/inspector_deepglobe_global_resnetfpn/inspector_deepglobe_global_resnetfpn.pth" \
--batch_size 6 \
--sub_batch_size 6 \
--patch_sizes 1224 996 640 \
--size 640 \
--origin_size 2448 \
--training_level -1 \
--level_decay 0 \
--workers 10