import argparse
import os
import datetime
import random
import time

import torch
import torch.optim as Optim

from torch.utils.data.dataloader import DataLoader
from tensorboardX import SummaryWriter

from dataset import ShapeNet,ShapeNet_PCN, ScaleEstimate
from models import SPM, ScaleEst
from metrics.metric import f_score, l1_cd, l2_cd
from metrics.loss import cd_loss_L1, emd_loss, scale_est_loss
from visualization import plot_pcd_one_view
import numpy as np


def make_dir(dir_path):
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


def log(fd,  message, time=True):
    if time:
        message = ' ==> '.join([datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), message])
    fd.write(message + '\n')
    fd.flush()
    print(message)


def prepare_logger(params):
    # prepare logger directory
    make_dir(params.log_dir)
    make_dir(os.path.join(params.log_dir, params.exp_name))

    logger_path = os.path.join(params.log_dir, params.exp_name, params.category)
    ckpt_dir = os.path.join(params.log_dir, params.exp_name, params.category, 'checkpoints')
    epochs_dir = os.path.join(params.log_dir, params.exp_name, params.category, 'epochs')

    make_dir(logger_path)
    make_dir(ckpt_dir)
    make_dir(epochs_dir)

    logger_file = os.path.join(params.log_dir, params.exp_name, params.category, 'logger.log')
    log_fd = open(logger_file, 'a')

    log(log_fd, "Experiment: {}".format(params.exp_name), False)
    log(log_fd, "Logger directory: {}".format(logger_path), False)
    log(log_fd, str(params), False)

    train_writer = SummaryWriter(os.path.join(logger_path, 'train'))
    val_writer = SummaryWriter(os.path.join(logger_path, 'val'))

    return ckpt_dir, epochs_dir, log_fd, train_writer, val_writer


def train(params):
    torch.backends.cudnn.benchmark = True

    ckpt_dir, epochs_dir, log_fd, train_writer, val_writer = prepare_logger(params)

    log(log_fd, 'Loading Data...')

    train_dataset = ShapeNet('data/House', 'train', params.category)
    val_dataset = ShapeNet('data/House', 'valid', params.category)

    train_dataloader = DataLoader(train_dataset, batch_size=params.batch_size, shuffle=True, num_workers=params.num_workers)
    val_dataloader = DataLoader(val_dataset, batch_size=params.batch_size, shuffle=False, num_workers=params.num_workers)
    # test
    test_dataset = ShapeNet('data/House', 'test', params.category)
    test_dataloader = DataLoader(test_dataset, batch_size=params.batch_size, shuffle=False, drop_last=True)
    log(log_fd, "Dataset loaded!")

    # model
    # --- SPM --- #
    # Normalized range
    size_range = [-1.0, -1.0, -1.0, 1.0, 1.0, 1.0]
    voxel_size = [2.0/32, 2.0/32, 2.0]
    max_num_pt = 20
    max_voxels = (32*32, 32*32)
    out_c_list = [128, 256, 512]
    outc = 512
    up_str  = [3,2,1]
    input_attr = 8
    channel = 128
    pseudo_num = 2576

    model = SPM(voxel_size, size_range, max_num_pt, max_voxels, input_attr, channel, channel, out_c_list, outc, up_str, outc, pseudo_num, latent_dim=512).to(params.device)

    # optimizer
    optimizer = Optim.Adam(model.parameters(), lr=params.lr, betas=(0.9, 0.999))
    lr_schedual = Optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.7)

    step = len(train_dataloader) // params.log_frequency

    # load pretrained model and optimizer
    if params.ckpt_path is not None:
        model.load_state_dict(torch.load(params.ckpt_path))
        print('model loaded!')
    
    # test
    if (params.mode == 'test'):
        print('\033[33m{:20s}{:20s}{:20s}{:20s}\033[0m'.format('Category', 'L1_CD(1e-3)', 'L2_CD(1e-4)', 'FScore-0.01(%)'))
        print('\033[33m{:20s}{:20s}{:20s}{:20s}\033[0m'.format('--------', '-----------', '-----------', '--------------'))

        model.eval()
        total_l1_cd, total_l2_cd, total_f_score = 0.0, 0.0, 0.0
        total_time = 0.0
        counter = 0
        with torch.no_grad():
            for p, c, center, scale in test_dataloader:
                p = p.to(params.device)
                c = c.to(params.device)
                center = center.to(params.device)
                scale = scale.to(params.device)

                torch.cuda.synchronize()
                time_start = time.time()
                _, c_, pred_, _ = model(p)
                torch.cuda.synchronize()
                time_end = time.time()
                time_sum = time_end - time_start
                total_time += time_sum
                counter += 1

                total_l1_cd += l1_cd(c_, c).item()
                total_l2_cd += l2_cd(c_, c).item()
                
                center = center.unsqueeze(1).repeat(1,8192,1)
                scale = scale.unsqueeze(-1).unsqueeze(-1).repeat(1,8192,3)

                for i in range(len(c)):
                    output_pc = c_[i].detach().cpu().numpy()
                    gt_pc = c[i].detach().cpu().numpy()
                    # --------------------------
                    total_f_score += f_score(output_pc, gt_pc)
        
        avg_l1_cd = total_l1_cd / len(test_dataset)
        avg_l2_cd = total_l2_cd / len(test_dataset)
        avg_f_score = total_f_score / len(test_dataset)
        avg_time = total_time / counter

        print('\033[33m{:20s}{:20s}{:20s}{:20s}\033[0m'.format('--------', '-----------', '-----------', '--------------'))
        print('\033[32m{:20s}{:<20.4f}{:<20.4f}{:<20.4f}\033[0m'.format('Average', np.mean(avg_l1_cd) * 1e3, np.mean(avg_l2_cd) * 1e4, np.mean(avg_f_score) * 1e2))
        print('average_inference_time:',avg_time,'ms')
    # training
    elif (params.mode == 'train'):
        best_cd_l1 = 1e8
        best_epoch_l1 = -1
        train_step, val_step = 0, 0
        for epoch in range(1, params.epochs + 1):
            # hyperparameter alpha
            if train_step < 10000:
                alpha = 0.01
            elif train_step < 20000:
                alpha = 0.1
            elif epoch < 50000:
                alpha = 0.5
            else:
                alpha = 1.0

            # training
            model.train()
            for i, (p, c, _, _) in enumerate(train_dataloader):
                p, c = p.to(params.device), c.to(params.device)

                optimizer.zero_grad()

                # forward propagation
                coarse_pred, dense_pred, pure_pred, inter_feature = model(p)
                
                # loss function
                if params.coarse_loss == 'cd':
                    loss1 = cd_loss_L1(coarse_pred, c) # Auxiliary Loss
                elif params.coarse_loss == 'emd':
                    coarse_c = c[:, :1024, :]
                    loss1 = emd_loss(coarse_pred, coarse_c)
                else:
                    raise ValueError('Not implemented loss {}'.format(params.coarse_loss))
                    
                loss2 = cd_loss_L1(dense_pred, c)
                loss = loss1 + alpha * loss2

                # back propagation
                loss.backward()
                optimizer.step()

                if (i + 1) % step == 0:
                    log(log_fd, "Training Epoch [{:03d}/{:03d}] - Iteration [{:03d}/{:03d}]: coarse loss = {:.6f}, dense l1 cd = {:.6f}, total loss = {:.6f}"
                        .format(epoch, params.epochs, i + 1, len(train_dataloader), loss1.item() * 1e3, loss2.item() * 1e3, loss.item() * 1e3))
                
                train_writer.add_scalar('coarse', loss1.item(), train_step)
                train_writer.add_scalar('dense', loss2.item(), train_step)
                train_writer.add_scalar('total', loss.item(), train_step)
                train_step += 1
            
            lr_schedual.step()

            # evaluation
            model.eval()
            total_cd_l1 = 0.0
            with torch.no_grad():
                rand_iter = random.randint(0, len(val_dataloader) - 1)  # for visualization

                for i, (p, c, _, _) in enumerate(val_dataloader):
                    p, c = p.to(params.device), c.to(params.device)
                    coarse_pred, dense_pred, pure_pred, _ = model(p)
                    total_cd_l1 += l1_cd(dense_pred, c).item()

                    # save into image
                    if rand_iter == i:
                        index = random.randint(0, dense_pred.shape[0] - 1)
                        plot_pcd_one_view(os.path.join(epochs_dir, 'epoch_{:03d}.png'.format(epoch)),
                                        [p[index].detach().cpu().numpy(), coarse_pred[index].detach().cpu().numpy(), dense_pred[index].detach().cpu().numpy(), c[index].detach().cpu().numpy()],
                                        ['Input', 'Coarse', 'Dense', 'Ground Truth'], xlim=(-0.35, 0.35), ylim=(-0.35, 0.35), zlim=(-0.35, 0.35))
                
                total_cd_l1 /= len(val_dataset)
                val_writer.add_scalar('l1_cd', total_cd_l1, val_step)
                val_step += 1

                log(log_fd, "Validate Epoch [{:03d}/{:03d}]: L1 Chamfer Distance = {:.6f}".format(epoch, params.epochs, total_cd_l1 * 1e3))
            
            if total_cd_l1 < best_cd_l1:
                best_epoch_l1 = epoch
                best_cd_l1 = total_cd_l1
                torch.save(model.state_dict(), os.path.join(ckpt_dir, 'best_l1_cd.pth'))
            
            if epoch == params.epochs:
                best_epoch_l1 = epoch
                best_cd_l1 = total_cd_l1
                torch.save(model.state_dict(), os.path.join(ckpt_dir, 'last_epoch.pth'))
            
            if epoch == 50:
                best_epoch_l1 = epoch
                best_cd_l1 = total_cd_l1
                torch.save(model.state_dict(), os.path.join(ckpt_dir, '50_epoch.pth'))
            
            if epoch == 100:
                best_epoch_l1 = epoch
                best_cd_l1 = total_cd_l1
                torch.save(model.state_dict(), os.path.join(ckpt_dir, '200_epoch.pth'))

            if epoch == 300:
                best_epoch_l1 = epoch
                best_cd_l1 = total_cd_l1
                torch.save(model.state_dict(), os.path.join(ckpt_dir, '300_epoch.pth'))
                
        log(log_fd, 'Best l1 cd model in epoch {}, the minimum l1 cd is {}'.format(best_epoch_l1, best_cd_l1 * 1e3))
        log_fd.close()

def train_est(params):
    torch.backends.cudnn.benchmark = True
    ckpt_dir, epochs_dir, log_fd, train_writer, val_writer = prepare_logger(params)

    log(log_fd, 'Loading Data...')

    train_dataset = ScaleEstimate('data/House', 'train', params.category)
    val_dataset = ScaleEstimate('data/House', 'valid', params.category)

    train_dataloader = DataLoader(train_dataset, batch_size=params.batch_size, shuffle=True, num_workers=params.num_workers)
    val_dataloader = DataLoader(val_dataset, batch_size=params.batch_size, shuffle=False, num_workers=params.num_workers)
    log(log_fd, "Dataset loaded!")
    # Scale Estimation Header
    seh = ScaleEst().to(params.device)

    # optimizer
    optimizer = Optim.Adam(seh.parameters(), lr=params.lr, betas=(0.9, 0.999))
    lr_schedual = Optim.lr_scheduler.StepLR(optimizer, step_size=50, gamma=0.7)

    step = len(train_dataloader) // params.log_frequency

    # load pretrained model and optimizer
    if params.ckpt_path is not None:
        seh.load_state_dict(torch.load(params.ckpt_path))
        print('model loaded!')
    
    if (params.mode == 'train'):
        train_step = 0
        for epoch in range(1, params.epochs + 1):
            seh.train()
            for i, (p, scale_gt, prob_gt) in enumerate(train_dataloader):
                p, scale_gt, prob_gt = p.to(params.device), scale_gt.to(params.device), prob_gt.to(params.device)
                
                optimizer.zero_grad()

                # forward propagation
                scale_pred, prob_pred = seh(p)
                loss = scale_est_loss(scale_pred, prob_pred, scale_gt, prob_gt)

                # back propagation
                loss.backward()
                optimizer.step()

                if (i + 1) % step == 0:
                        log(log_fd, "Training Epoch [{:03d}/{:03d}] - Iteration [{:03d}/{:03d}]: scale loss = {:.6f}"
                            .format(epoch, params.epochs, i + 1, len(train_dataloader), loss.item()))
                    
                train_writer.add_scalar('scale_loss', loss.item(), train_step)
                train_step += 1
            
            lr_schedual.step()
        
        if epoch == params.epochs:
            torch.save(seh.state_dict(), os.path.join(ckpt_dir, 'last_epoch.pth'))
        log_fd.close()

    # evaluation
    if (params.mode == 'test'):
        seh.eval()
        count = 0
        total_time_se = 0.0
        para_case = total_case = success_case = success_final = 0
        x_scale = y_scale = z_scale = 0.0
        with torch.no_grad():
            for p, scale_gt, prob_gt in val_dataloader:
                p, scale_gt, prob_gt = p.to(params.device), scale_gt.to(params.device), prob_gt.to(params.device)
                torch.cuda.synchronize()
                time_start_se = time.time()
                s_p, p_p = seh(p)
                torch.cuda.synchronize()
                time_end_se = time.time()
                time_sum_se = time_end_se - time_start_se
                total_time_se += time_sum_se

                # Statistics
                for i in range(scale_gt.shape[0]):
                    total_case += 1
                    para_case += 1
                    x_scale += s_p[i,0]
                    y_scale += s_p[i,1]
                    z_scale += s_p[i,2]

                    max_p = np.max(s_p[i].cpu().numpy(), axis=-1)
                    max_gt = np.max(scale_gt[i].cpu().numpy(), axis=-1)
                    if (max_p-max_gt>0 and max_p-max_gt < 15):
                        success_final += 1

                    if (s_p[i,0]-scale_gt[i,0] > 0 and s_p[i,1]-scale_gt[i,1] > 0 and s_p[i,2]-scale_gt[i,2] > 0):
                        success_case += 1

                count += 1
            
        avg_time = total_time_se / count
        success_rate = success_case / total_case
        success_rate_final = success_final / para_case
        x_avg = x_scale / para_case
        y_avg = y_scale / para_case
        z_avg = z_scale / para_case
        print('average_inference_time:',avg_time,'ms')
        print('scale estimation success rate:', success_rate)
        print('scale estimation final success rate:', success_rate_final)
        print('avg x:', x_avg, 'm')
        print('avg y:', y_avg, 'm')
        print('avg z:', z_avg, 'm')


if __name__ == '__main__':
    parser = argparse.ArgumentParser('SPM')
    parser.add_argument('--exp_name', type=str, help='Tag of experiment')
    parser.add_argument('--log_dir', type=str, default='log', help='Logger directory')
    parser.add_argument('--ckpt_path', type=str, default=None, help='The path of pretrained model')
    parser.add_argument('--lr', type=float, default=0.0001, help='Learning rate')
    parser.add_argument('--category', type=str, default='all', help='Category of point clouds')
    parser.add_argument('--epochs', type=int, default=200, help='Epochs of training')
    parser.add_argument('--batch_size', type=int, default=32, help='Batch size for data loader')
    parser.add_argument('--coarse_loss', type=str, default='cd', help='loss function for coarse point cloud')
    parser.add_argument('--num_workers', type=int, default=6, help='num_workers for data loader')
    parser.add_argument('--device', type=str, default='cuda:0', help='device for training')
    parser.add_argument('--log_frequency', type=int, default=10, help='Logger frequency in every epoch')
    parser.add_argument('--save_frequency', type=int, default=10, help='Model saving frequency')
    parser.add_argument('--mode', type=str, default='train', help='mode for network')
    parser.add_argument('--header', type=str, default='sph', help='header for network')
    params = parser.parse_args()
    
    if (params.header == 'sph'):
        train(params)
    elif (params.header == 'seh'):
        train_est(params)
