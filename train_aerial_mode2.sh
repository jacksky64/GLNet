#export CUDA_VISIBLE_DEVICES=3,4,5
# python -m torch.distributed.launch --nproc_per_node=8 --use_env train_aerial.py \
# --n_class 2 \
# --data_path "/vinai/chuonghm/aerial" \
# --model_path "/vinai/chuonghm/glnet/saved_models" \
# --log_path "/vinai/chuonghm/glnet/logs" \
# --task_name "fpn_aerial_global_new" \
# --mode 1 \
# --batch_size 3 \
# --sub_batch_size 6 \
# --size_g 536 \
# --size_p 536 \
# --workers 6 --world-size 8

python -m torch.distributed.launch --nproc_per_node=8 --use_env train_aerial.py \
--n_class 2 \
--data_path "/vinai/chuonghm/aerial" \
--model_path "/vinai/chuonghm/glnet/saved_models" \
--log_path "/vinai/chuonghm/glnet/logs" \
--task_name "fpn_aerial_global2local_new" \
--mode 2 \
--batch_size 3 \
--sub_batch_size 3 \
--size_g 536 \
--size_p 536 \
--path_g "fpn_aerial_global_new.pth" \
--dist-url "tcp://0.0.0.0:1234" \
--workers 6 --world-size 8