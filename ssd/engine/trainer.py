import collections
import datetime
import logging
import os
import time
import torch
import torch.distributed as dist

from ssd.engine.inference import do_evaluation
from ssd.utils import dist_util
from ssd.utils.metric_logger import MetricLogger


def write_metric(eval_result, prefix, summary_writer, global_step):
    for key in eval_result:
        value = eval_result[key]
        tag = '{}/{}'.format(prefix, key)
        if isinstance(value, collections.Mapping):
            write_metric(value, tag, summary_writer, global_step)
        else:
            summary_writer.add_scalar(tag, value, global_step=global_step)


def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = dist_util.get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses

from math import cos, pi
def adjust_learning_rate(optimizer, iteration, num_iter, warmup_iter, warmup_factor):
    lr_base = optimizer.param_groups[0]['lr']

    # warmup_epoch = 5 if args.warmup else 0
    # warmup_iter = warmup_epoch * num_iter
    current_iter = iteration
    max_iter = num_iter

    lr = lr_base * (1 + cos(pi * (current_iter - warmup_iter) / (max_iter - warmup_iter))) / 2

    if current_iter < warmup_iter:
        alpha = float(current_iter) / warmup_iter
        warmup_factor = warmup_factor * (1 - alpha) + alpha
        
        lr = lr_base * warmup_factor
        print(current_iter, warmup_iter, warmup_factor, lr_base, lr, alpha)

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

def do_train(cfg, model,
             data_loader,
             optimizer,
             scheduler,
             checkpointer,
             device,
             arguments,
             args):
    logger = logging.getLogger("SSD.trainer")
    logger.info("Start training ...")
    meters = MetricLogger()

    model.train()
    save_to_disk = dist_util.get_rank() == 0
    if args.use_tensorboard and save_to_disk:
        import tensorboardX

        summary_writer = tensorboardX.SummaryWriter(log_dir=os.path.join(cfg.OUTPUT_DIR, 'tf_logs'))
    else:
        summary_writer = None

    max_iter = len(data_loader)
    start_iter = arguments["iteration"]
    start_training_time = time.time()
    end = time.time()
    # print(len(data_loader))
    # num_iter = len(data_loader)
    for iteration, (images, targets, _) in enumerate(data_loader, start_iter):
        iteration = iteration + 1
        arguments["iteration"] = iteration
        
        # adjust_learning_rate(optimizer, iteration, num_iter, cfg.SOLVER.WARMUP_ITERS, cfg.SOLVER.WARMUP_FACTOR)

        images = images.to(device)
        targets = targets.to(device)
        loss_dict = model(images, targets=targets)
        loss = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(total_loss=losses_reduced, **loss_dict_reduced)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time)
        if iteration % args.log_step == 0:
            eta_seconds = meters.time.global_avg * (max_iter - iteration)
            eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))
            meters_data = {
                'iter':   iteration,
                'lr':     optimizer.param_groups[0]['lr'],
                'meters': str(meters),
                'eta':    eta_string,
            }
            meters_fields = [
                "iter: {iter:06d}",
                "lr: {lr:.5f}",
                "{meters}",
                "eta: {eta}",
            ]
            if torch.cuda.is_available():
                mem = torch.cuda.max_memory_allocated()
                meters_data['mem'] = round(mem / 1024. / 1024.)
                meters_fields.append("mem: {mem}M")
            logger.info(
                meters.delimiter.join(meters_fields).format(**meters_data)
            )
            if summary_writer:
                global_step = iteration
                summary_writer.add_scalar('losses/total_loss', losses_reduced, global_step=global_step)
                for loss_name, loss_item in loss_dict_reduced.items():
                    summary_writer.add_scalar('losses/{}'.format(loss_name), loss_item, global_step=global_step)
                summary_writer.add_scalar('lr', optimizer.param_groups[0]['lr'], global_step=global_step)

        if iteration % args.save_step == 0:
            checkpointer.save("model_{:06d}".format(iteration), **arguments)

        if args.eval_step > 0 and iteration % args.eval_step == 0 and not iteration == max_iter:
            eval_results = do_evaluation(cfg, model, distributed=args.distributed, iteration=iteration)
            if dist_util.get_rank() == 0 and summary_writer:
                for eval_result, dataset in zip(eval_results, cfg.DATASETS.TEST):
                    write_metric(eval_result['metrics'], 'metrics/' + dataset, summary_writer, iteration)
            model.train()  # *IMPORTANT*: change to train mode after eval.

    checkpointer.save("model_final", **arguments)
    # compute training time
    total_training_time = int(time.time() - start_training_time)
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    logger.info("Total training time: {} ({:.4f} s / it)".format(total_time_str, total_training_time / max_iter))
    return model
