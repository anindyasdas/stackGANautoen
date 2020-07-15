from __future__ import print_function
from six.moves import range

import torch.backends.cudnn as cudnn
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.optim as optim
import torchvision.utils as vutils
import numpy as np
import os
import time
from PIL import Image, ImageFont, ImageDraw
from copy import deepcopy

from miscc.config import cfg
from miscc.utils import mkdir_p

from tensorboardX import summary
from tensorboardX import FileWriter

from model1 import encoder2, G_NET1, D_NET64, D_NET128, D_NET256, D_NET512, D_NET1024, INCEPTION_V3


device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
finetuned = None
# ################## Shared functions ###################
def compute_mean_covariance(img):
    batch_size = img.size(0)
    channel_num = img.size(1)
    height = img.size(2)
    width = img.size(3)
    num_pixels = height * width

    # batch_size * channel_num * 1 * 1
    mu = img.mean(2, keepdim=True).mean(3, keepdim=True)

    # batch_size * channel_num * num_pixels
    img_hat = img - mu.expand_as(img)
    img_hat = img_hat.view(batch_size, channel_num, num_pixels)
    # batch_size * num_pixels * channel_num
    img_hat_transpose = img_hat.transpose(1, 2)
    # batch_size * channel_num * channel_num
    covariance = torch.bmm(img_hat, img_hat_transpose)
    covariance = covariance / num_pixels

    return mu, covariance


def KL_loss(mu, logvar):
    # -0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
    KLD = torch.mean(KLD_element).mul_(-0.5)
    return KLD


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        nn.init.orthogonal(m.weight.data, 1.0)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)
    elif classname.find('Linear') != -1:
        nn.init.orthogonal(m.weight.data, 1.0)
        if m.bias is not None:
            m.bias.data.fill_(0.0)


def load_params(model, new_param):
    for p, new_p in zip(model.parameters(), new_param):
        p.data.copy_(new_p)


def copy_G_params(model):
    flatten = deepcopy(list(p.data for p in model.parameters()))
    return flatten


def compute_inception_score(predictions, num_splits=1):
    # print('predictions', predictions.shape)
    scores = []
    for i in range(num_splits):
        istart = i * predictions.shape[0] // num_splits
        iend = (i + 1) * predictions.shape[0] // num_splits
        part = predictions[istart:iend, :]
        kl = part * \
            (np.log(part) - np.log(np.expand_dims(np.mean(part, 0), 0)))
        kl = np.mean(np.sum(kl, 1))
        scores.append(np.exp(kl))
    return np.mean(scores), np.std(scores)


def negative_log_posterior_probability(predictions, num_splits=1):
    # print('predictions', predictions.shape)
    scores = []
    for i in range(num_splits):
        istart = i * predictions.shape[0] // num_splits
        iend = (i + 1) * predictions.shape[0] // num_splits
        part = predictions[istart:iend, :]
        result = -1. * np.log(np.max(part, 1))
        result = np.mean(result)
        scores.append(result)
    return np.mean(scores), np.std(scores)




def load_network(gpus, path):
    enc = encoder2()
    #enc, input_size = img_models.initialize_torchvision_model(model_name, ft_vector_dim, feature_extract, device=device, use_pretrained=use_pretrained, vae=vae)
    #enc = torch.nn.DataParallel(enc, device_ids=gpus)
    #enc.apply(weights_init)
    netG = G_NET1()
    netG.apply(weights_init)
    netG = torch.nn.DataParallel(netG, device_ids=gpus)
    print(netG)
    if finetuned:
        print('loading from previous checkpoint.....')
        enc.load_state_dict(torch.load(finetuned))
    enc = enc.to(device)

    netsD = []
    if cfg.TREE.BRANCH_NUM > 0:
        netsD.append(D_NET64())
    if cfg.TREE.BRANCH_NUM > 1:
        netsD.append(D_NET128())
    if cfg.TREE.BRANCH_NUM > 2:
        netsD.append(D_NET256())
    if cfg.TREE.BRANCH_NUM > 3:
        netsD.append(D_NET512())
    if cfg.TREE.BRANCH_NUM > 4:
        netsD.append(D_NET1024())
    # TODO: if cfg.TREE.BRANCH_NUM > 5:

    for i in range(len(netsD)):
        netsD[i].apply(weights_init)
        netsD[i] = torch.nn.DataParallel(netsD[i], device_ids=gpus)
        # print(netsD[i])
    print('# of netsD', len(netsD))

    count = 0
    if cfg.TRAIN.NET_G != '':
        # example cfg.TRAIN.NET_G = 
        Gpath = os.path.join(path, cfg.TRAIN.NET_G )
        checkpoint = torch.load(Gpath)
        netG.load_state_dict(checkpoint['state_dict'])
        
        print('Load ', cfg.TRAIN.NET_G)

        istart = cfg.TRAIN.NET_G.rfind('_') + 1
        iend = cfg.TRAIN.NET_G.rfind('.')
        count = cfg.TRAIN.NET_G[istart:iend]
        Epath = os.path.join(path, 'encG_%d.pth' %int(count))
        checkpoint = torch.load(Epath)
        enc.load_state_dict(checkpoint['state_dict'])
        enc.train()
        netG.train()
        count = int(count) + 1

    if cfg.TRAIN.NET_D != '':
        for i in range(len(netsD)):
            Dpath = os.path.join(path, '%s%d.pth' % (cfg.TRAIN.NET_D, i))
            print('Load %s_%d.pth' % (cfg.TRAIN.NET_D, i))
            checkpoint = torch.load(Dpath)
            netsD[i].load_state_dict(checkpoint['state_dict'])
            netsD[i].train()

    inception_model = INCEPTION_V3()

    if cfg.CUDA:
        enc.cuda()
        netG.cuda()
        for i in range(len(netsD)):
            netsD[i].cuda()
        inception_model = inception_model.cuda()
    inception_model.eval()

    return enc, netG, netsD, len(netsD), inception_model, count

def optimizerToDevice(optimizer):
    for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)
    return optimizer

def define_optimizers(enc, netG, netsD, path):
    optimizersD = []
    num_Ds = len(netsD)
    for i in range(num_Ds):
        opt = optim.Adam(netsD[i].parameters(),
                         lr=cfg.TRAIN.DISCRIMINATOR_LR,
                         betas=(0.5, 0.999))
        optimizersD.append(opt)

    # G_opt_paras = []
    # for p in netG.parameters():
    #     if p.requires_grad:
    #         G_opt_paras.append(p)
    
    optimizerG = optim.Adam( list(enc.parameters())+list(netG.parameters()) ,
                            lr=cfg.TRAIN.GENERATOR_LR,
                            betas=(0.5, 0.999))
    """
    optimizerG = optim.Adam( netG.parameters() ,
                            lr=cfg.TRAIN.GENERATOR_LR,
                            betas=(0.5, 0.999))
    """
    
    if cfg.TRAIN.NET_G != '':
        Gpath = os.path.join(path, cfg.TRAIN.NET_G )
        print('loading optimizer from ', Gpath)
        checkpoint = torch.load(Gpath)
        optimizerG.load_state_dict(checkpoint['optimizer'])
        optimizerG = optimizerToDevice(optimizerG)
    if cfg.TRAIN.NET_D != '':
        for i in range(num_Ds):
            Dpath = os.path.join(path, '%s%d.pth' % (cfg.TRAIN.NET_D, i))
            checkpoint = torch.load(Dpath)
            print('loading optimizer from ', Dpath)
            optimizersD[i].load_state_dict(checkpoint['optimizer'])
            optimizersD[i] = optimizerToDevice(optimizersD[i])
        
        
        
    return optimizerG, optimizersD


def save_model(enc, avg_param_E, netG, optimizerG, avg_param_G, netsD, optimizersD, epoch, model_dir):
    load_params(netG, avg_param_G)
    load_params(enc, avg_param_E)
    
    stateE = {'state_dict': enc.state_dict(),
             'optimizer': optimizerG.state_dict()}
    
    torch.save(
        stateE,
        '%s/encG_%d.pth' % (model_dir, epoch))
    
    
    stateG = {'state_dict': netG.state_dict(),
             'optimizer': optimizerG.state_dict()}
    torch.save(
        stateG,
        '%s/netG_%d.pth' % (model_dir, epoch))
    
    for i in range(len(netsD)):
        netD = netsD[i]
        netD = netsD[i]
        stateD = {'state_dict':  netD.state_dict(),
             'optimizer': optimizersD[i].state_dict()}
        torch.save(
            stateD,
            '%s/netD%d.pth' % (model_dir, i))
    print('Save G/Ds models.')


def save_img_results(imgs_tcpu, fake_imgs, num_imgs,
                     count, image_dir, summary_writer):
    num = cfg.TRAIN.VIS_COUNT

    # The range of real_img (i.e., self.imgs_tcpu[i][0:num])
    # is changed to [0, 1] by function vutils.save_image
    real_img = imgs_tcpu[-1][0:num]
    vutils.save_image(
        real_img, '%s/real_samples.png' % (image_dir),
        normalize=True)
    real_img_set = vutils.make_grid(real_img).numpy()
    real_img_set = np.transpose(real_img_set, (1, 2, 0))
    real_img_set = real_img_set * 255
    real_img_set = real_img_set.astype(np.uint8)
    sup_real_img = summary.image('real_img', real_img_set, dataformats='HWC')
    print('real image saved')
    summary_writer.add_summary(sup_real_img, count)
    
    print('generated image saved')
    for i in range(num_imgs):
        fake_img = fake_imgs[i][0:num]
        # The range of fake_img.data (i.e., self.fake_imgs[i][0:num])
        # is still [-1. 1]...
        vutils.save_image(
            fake_img.data, '%s/count_%09d_fake_samples%d.png' %
            (image_dir, count, i), normalize=True)

        fake_img_set = vutils.make_grid(fake_img.data).cpu().numpy()

        fake_img_set = np.transpose(fake_img_set, (1, 2, 0))
        fake_img_set = (fake_img_set + 1) * 255 / 2
        fake_img_set = fake_img_set.astype(np.uint8)

        sup_fake_img = summary.image('fake_img%d' % i, fake_img_set, dataformats='HWC')
        summary_writer.add_summary(sup_fake_img, count)
        summary_writer.flush()




# ################# Text to image task############################ #
class condGANTrainer(object):
    def __init__(self, output_dir, data_loader, imsize):
        if cfg.TRAIN.FLAG:
            self.model_dir = os.path.join(output_dir, 'Model')
            self.image_dir = os.path.join(output_dir, 'Image')
            self.log_dir = os.path.join(output_dir, 'Log')
            mkdir_p(self.model_dir)
            mkdir_p(self.image_dir)
            mkdir_p(self.log_dir)
            self.summary_writer = FileWriter(self.log_dir)

        s_gpus = cfg.GPU_ID.split(',')
        self.gpus = [int(ix) for ix in s_gpus]
        self.num_gpus = len(self.gpus)
        torch.cuda.set_device(self.gpus[0])
        cudnn.benchmark = True

        self.batch_size = cfg.TRAIN.BATCH_SIZE * self.num_gpus
        self.max_epoch = cfg.TRAIN.MAX_EPOCH
        self.snapshot_interval = cfg.TRAIN.SNAPSHOT_INTERVAL

        self.data_loader = data_loader
        self.num_batches = len(self.data_loader)

    def prepare_data(self, data):
        uimgs, imgs, w_imgs, t_embedding, _ = data

        real_vimgs, wrong_vimgs, ureal_vimgs = [], [], []
        if cfg.CUDA:
            vembedding = Variable(t_embedding).cuda()
        else:
            vembedding = Variable(t_embedding)
        for i in range(self.num_Ds):
            if cfg.CUDA:
                real_vimgs.append(Variable(imgs[i]).cuda())
                wrong_vimgs.append(Variable(w_imgs[i]).cuda())
                ureal_vimgs.append(Variable(uimgs[i]).cuda())
            else:
                real_vimgs.append(Variable(imgs[i]))
                wrong_vimgs.append(Variable(w_imgs[i]))
                ureal_vimgs.append(Variable(uimgs[i]).cuda())
        return imgs, ureal_vimgs, real_vimgs, wrong_vimgs, vembedding

    def train_Dnet(self, idx, count):
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion, mu = self.criterion, self.mu

        netD, optD = self.netsD[idx], self.optimizersD[idx]
        real_imgs = self.real_imgs[idx]
        wrong_imgs = self.wrong_imgs[idx]
        fake_imgs = self.fake_imgs[idx]
        #
        netD.zero_grad()
        # Forward
        real_labels = self.real_labels[:batch_size]
        fake_labels = self.fake_labels[:batch_size]
        # for real
        real_logits = netD(real_imgs, mu.detach())
        wrong_logits = netD(wrong_imgs, mu.detach())
        fake_logits = netD(fake_imgs.detach(), mu.detach())
        #
        errD_real = criterion(real_logits[0], real_labels)
        errD_wrong = criterion(wrong_logits[0], fake_labels)
        errD_fake = criterion(fake_logits[0], fake_labels)
        if len(real_logits) > 1 and cfg.TRAIN.COEFF.UNCOND_LOSS > 0:
            errD_real_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(real_logits[1], real_labels)
            errD_wrong_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(wrong_logits[1], real_labels)
            errD_fake_uncond = cfg.TRAIN.COEFF.UNCOND_LOSS * \
                criterion(fake_logits[1], fake_labels)
            #
            errD_real = errD_real + errD_real_uncond
            errD_wrong = errD_wrong + errD_wrong_uncond
            errD_fake = errD_fake + errD_fake_uncond
            #
            errD = errD_real + errD_wrong + errD_fake
        else:
            errD = errD_real + 0.5 * (errD_wrong + errD_fake)
        # backward
        errD.backward()
        # update parameters
        optD.step()
        # log
        if flag == 0:
            summary_D = summary.scalar('D_loss%d' % idx, errD.item())
            self.summary_writer.add_summary(summary_D, count)
        return errD

    def train_Gnet(self, count):
        self.enc.zero_grad()
        self.netG.zero_grad()
        errG_total = 0
        errM_total = 0
        flag = count % 100
        batch_size = self.real_imgs[0].size(0)
        criterion, criterion1, mu, logvar = self.criterion, self.criterion1, self.mu, self.logvar
        real_labels = self.real_labels[:batch_size]
        for i in range(self.num_Ds):
            outputs = self.netsD[i](self.fake_imgs[i], mu)
            errG = criterion(outputs[0], real_labels)
            errM = criterion1(self.fake_imgs[i], self.real_imgs[i]) *50
            if len(outputs) > 1 and cfg.TRAIN.COEFF.UNCOND_LOSS > 0:
                errG_patch = cfg.TRAIN.COEFF.UNCOND_LOSS *\
                    criterion(outputs[1], real_labels)
                errG = errG + errG_patch
            errG_total = errG_total + errG
            errM_total = errM_total + errM
            if flag == 0:
                summary_D = summary.scalar('G_loss%d' % i, errG.item())
                self.summary_writer.add_summary(summary_D, count)

        # Compute color consistency losses
        if cfg.TRAIN.COEFF.COLOR_LOSS > 0:
            if self.num_Ds > 1:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-1])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-2].detach())
                like_mu2 = cfg.TRAIN.COEFF.COLOR_LOSS * criterion1(mu1, mu2)
                like_cov2 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    criterion1(covariance1, covariance2)
                errG_total = errG_total + like_mu2 + like_cov2
                if flag == 0:
                    sum_mu = summary.scalar('G_like_mu2', like_mu2.item())
                    self.summary_writer.add_summary(sum_mu, count)
                    sum_cov = summary.scalar('G_like_cov2', like_cov2.item())
                    self.summary_writer.add_summary(sum_cov, count)
            if self.num_Ds > 2:
                mu1, covariance1 = compute_mean_covariance(self.fake_imgs[-2])
                mu2, covariance2 = \
                    compute_mean_covariance(self.fake_imgs[-3].detach())
                like_mu1 = cfg.TRAIN.COEFF.COLOR_LOSS * criterion1(mu1, mu2)
                like_cov1 = cfg.TRAIN.COEFF.COLOR_LOSS * 5 * \
                    criterion1(covariance1, covariance2)
                errG_total = errG_total + like_mu1 + like_cov1
                if flag == 0:
                    sum_mu = summary.scalar('G_like_mu1', like_mu1.item())
                    self.summary_writer.add_summary(sum_mu, count)
                    sum_cov = summary.scalar('G_like_cov1', like_cov1.item())
                    self.summary_writer.add_summary(sum_cov, count)

        kl_loss = KL_loss(mu, logvar) * cfg.TRAIN.COEFF.KL
        
        
       
        emb_MSE_loss = criterion1(self.txt_embedding_cyl, self.txt_embedding.detach()) * 50 * 2
        mu_MSE_loss = criterion1(self.mu_cyl, self.mu.detach()) * 50
        logvar_MSE_loss = criterion1(self.logvar_cyl, self.logvar.detach()) * 5 * 50
        emb_loss = emb_MSE_loss + mu_MSE_loss + logvar_MSE_loss
        err = errG_total + kl_loss + emb_loss + errM_total
        err.backward()
        torch.nn.utils.clip_grad_norm_(self.netG.parameters(), 5.00)
        torch.nn.utils.clip_grad_norm_(self.enc.parameters(), 5.00)
        self.optimizerG.step()
        return kl_loss, errG_total, emb_loss, errM_total

    def train(self):
        self.enc, self.netG, self.netsD, self.num_Ds,\
            self.inception_model, start_count = load_network(self.gpus, self.model_dir)
        avg_param_G = copy_G_params(self.netG)
        avg_param_E = copy_G_params(self.enc)

        self.optimizerG, self.optimizersD = \
            define_optimizers(self.enc, self.netG, self.netsD, self.model_dir)

        self.criterion = nn.BCELoss()
        self.criterion1 = nn.MSELoss()

        self.real_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(1))
        self.fake_labels = \
            Variable(torch.FloatTensor(self.batch_size).fill_(0))

        self.gradient_one = torch.FloatTensor([1.0])
        self.gradient_half = torch.FloatTensor([0.5])

        nz = cfg.GAN.Z_DIM
        noise = Variable(torch.FloatTensor(self.batch_size, nz))
        fixed_noise = \
            Variable(torch.FloatTensor(self.batch_size, nz).normal_(0, 1))

        if cfg.CUDA:
            self.criterion.cuda()
            self.criterion1.cuda()
            self.real_labels = self.real_labels.cuda()
            self.fake_labels = self.fake_labels.cuda()
            self.gradient_one = self.gradient_one.cuda()
            self.gradient_half = self.gradient_half.cuda()
            noise, fixed_noise = noise.cuda(), fixed_noise.cuda()

        predictions = []
        count = start_count
        start_epoch = start_count // (self.num_batches)
        #self.enc.eval()
        for epoch in range(start_epoch, self.max_epoch):
            start_t = time.time()

            for step, data in enumerate(self.data_loader, 0):
                #######################################################
                # (0) Prepare training data
                ######################################################
                self.imgs_tcpu, self.ureal_imgs, self.real_imgs, self.wrong_imgs, \
                    self.txt_embed = self.prepare_data(data)
                self.txt_embedding, self.mu, self.logvar = self.enc(self.real_imgs[0])
                #print('shape of embed:', self.txt_embedding.shape)

                #######################################################
                # (1) Generate fake images
                ######################################################
                noise.data.normal_(0, 1)
                self.fake_imgs = self.netG(noise, self.txt_embedding)
                #print('shape of fake0:', self.fake_imgs[0].shape)
                #print('shape of fake1:', self.fake_imgs[1].shape)
                #print('shape of fake2:', self.fake_imgs[2].shape)
                #################Encoding from Generated image##########
                self.txt_embedding_cyl, self.mu_cyl, self.logvar_cyl = self.enc(self.fake_imgs[0])
                

                #######################################################
                # (2) Update D network
                ######################################################
                errD_total = 0
                for i in range(self.num_Ds):
                    errD = self.train_Dnet(i, count)
                    errD_total += errD

                #######################################################
                # (3) Update G network: maximize log(D(G(z)))
                ######################################################
                kl_loss, errG_total, emb_loss, errM_total = self.train_Gnet(count)
                for p, avg_p in zip(self.netG.parameters(), avg_param_G):
                    avg_p.mul_(0.999).add_(0.001, p.data)
                for e, avg_e in zip(self.enc.parameters(), avg_param_E):
                    avg_e.mul_(0.999).add_(0.001, e.data)

                # for inception score
                pred = self.inception_model(self.fake_imgs[-1].detach())
                predictions.append(pred.data.cpu().numpy())

                if count % 100 == 0:
                    summary_D = summary.scalar('D_loss', errD_total.item())
                    summary_G = summary.scalar('G_loss', errG_total.item())
                    summary_KL = summary.scalar('KL_loss', kl_loss.item())
                    summary_emb = summary.scalar('EmbeddingMSE_loss', emb_loss.item())
                    summary_M = summary.scalar('MSE_loss', errM_total.item())
                    self.summary_writer.add_summary(summary_D, count)
                    self.summary_writer.add_summary(summary_G, count)
                    self.summary_writer.add_summary(summary_KL, count)
                    self.summary_writer.add_summary(summary_emb, count)
                    self.summary_writer.add_summary(summary_M, count)

                count = count + 1

                if count % cfg.TRAIN.SNAPSHOT_INTERVAL == 0:
                #if count % 2 == 0:
                    save_model(self.enc, avg_param_E, self.netG, self.optimizerG, avg_param_G, self.netsD, self.optimizersD, count, self.model_dir)
                    # Save images
                    backup_para = copy_G_params(self.netG)
                    backup_para_E = copy_G_params(self.enc)
                    load_params(self.netG, avg_param_G)
                    load_params(self.enc, avg_param_E)
                    #
                    self.fake_imgs = self.netG(fixed_noise, self.txt_embedding)
                    save_img_results(self.imgs_tcpu, self.fake_imgs, self.num_Ds,
                                     count, self.image_dir, self.summary_writer)
                    #
                    load_params(self.netG, backup_para)
                    load_params(self.enc, backup_para_E)

                    # Compute inception score
                    if len(predictions) > 500:
                        predictions = np.concatenate(predictions, 0)
                        mean, std = compute_inception_score(predictions, 10)
                        # print('mean:', mean, 'std', std)
                        m_incep = summary.scalar('Inception_mean', mean)
                        self.summary_writer.add_summary(m_incep, count)
                        #
                        mean_nlpp, std_nlpp = \
                            negative_log_posterior_probability(predictions, 10)
                        m_nlpp = summary.scalar('NLPP_mean', mean_nlpp)
                        self.summary_writer.add_summary(m_nlpp, count)
                        #
                        predictions = []

            end_t = time.time()
            print('''[%d/%d][%d]
                         Loss_D: %.2f Loss_G: %.2f Loss_KL: %.2f Loss_emb: %.2f Loss_MSE: %.2f Time: %.2fs
                      '''  # D(real): %.4f D(wrong):%.4f  D(fake) %.4f
                  % (epoch, self.max_epoch, self.num_batches,
                     errD_total.item(), errG_total.item(),
                     kl_loss.item(), emb_loss.item(), errM_total.item(), end_t - start_t))

        save_model(self.enc, avg_param_E, self.netG, self.optimizerG, avg_param_G, self.netsD, self.optimizersD, count, self.model_dir)
        self.summary_writer.close()

    def save_superimages(self, images_list, filenames,
                         save_dir, split_dir, imsize):
        batch_size = images_list[0].size(0)
        num_sentences = len(images_list)
        for i in range(batch_size):
            s_tmp = '%s/super/%s/%s' %\
                (save_dir, split_dir, filenames[i])
            folder = s_tmp[:s_tmp.rfind('/')]
            if not os.path.isdir(folder):
                print('Make a new folder: ', folder)
                mkdir_p(folder)
            #
            savename = '%s_%d.png' % (s_tmp, imsize)
            super_img = []
            for j in range(num_sentences):
                img = images_list[j][i]
                # print(img.size())
                img = img.view(1, 3, imsize, imsize)
                # print(img.size())
                super_img.append(img)
                # break
            super_img = torch.cat(super_img, 0)
            vutils.save_image(super_img, savename, nrow=10, normalize=True)

    def save_singleimages(self, images, filenames,
                          save_dir, split_dir, sentenceID, imsize):
        for i in range(images.size(0)):
            s_tmp = '%s/single_samples/%s/%s' %\
                (save_dir, split_dir, filenames[i])
            folder = s_tmp[:s_tmp.rfind('/')]
            if not os.path.isdir(folder):
                print('Make a new folder: ', folder)
                mkdir_p(folder)

            fullpath = '%s_%d_sentence%d.png' % (s_tmp, imsize, sentenceID)
            # range from [-1, 1] to [0, 255]
            img = images[i].add(1).div(2).mul(255).clamp(0, 255).byte()
            ndarr = img.permute(1, 2, 0).data.cpu().numpy()
            im = Image.fromarray(ndarr)
            im.save(fullpath)

   