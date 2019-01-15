import time
import math
import os
import sys
if len(sys.argv) < 2:
    print('Usage: {} datasource'.format(sys.argv[0]))
    print('\tAvailable datasources: boxes, minipong, mediumpong...')
    exit(1)

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

import imutil
from logutil import TimeSeries
from tqdm import tqdm
from spatial_recurrent import CSRN
from coordconv import CoordConv2d

from higgins import higgins_metric

from importlib import import_module
datasource = import_module('envs.' + sys.argv[1])

import models

from causal_graph import render_causal_graph


# Inverse multiquadratic kernel with varying kernel bandwidth
# Tolstikhin et al. https://arxiv.org/abs/1711.01558
# https://github.com/schelotto/Wasserstein_Autoencoders
def imq_kernel(X: torch.Tensor,
               Y: torch.Tensor,
               h_dim: int):
    batch_size = X.size(0)

    p2_norm_x = X.pow(2).sum(1).unsqueeze(0)
    norms_x = X.sum(1).unsqueeze(0)
    prods_x = torch.mm(norms_x, norms_x.t())
    dists_x = p2_norm_x + p2_norm_x.t() - 2 * prods_x

    p2_norm_y = Y.pow(2).sum(1).unsqueeze(0)
    norms_y = X.sum(1).unsqueeze(0)
    prods_y = torch.mm(norms_y, norms_y.t())
    dists_y = p2_norm_y + p2_norm_y.t() - 2 * prods_y

    dot_prd = torch.mm(norms_x, norms_y.t())
    dists_c = p2_norm_x + p2_norm_y.t() - 2 * dot_prd

    stats = 0
    for scale in [.1, .2, .5, 1., 2., 5., 10.]:
        C = 2 * h_dim * 1.0 * scale
        res1 = C / (C + dists_x)
        res1 += C / (C + dists_y)

        if torch.cuda.is_available():
            res1 = (1 - torch.eye(batch_size).cuda()) * res1
        else:
            res1 = (1 - torch.eye(batch_size)) * res1

        res1 = res1.sum() / (batch_size - 1)
        res2 = C / (C + dists_c)
        res2 = res2.sum() * 2. / (batch_size)
        stats += res1 - res2
    return stats


# Maximum Mean Discrepancy between z and a reference distribution
# This term goes to zero if z is perfectly normal (with variance sigma**2)
def mmd_normal_penalty(z, sigma=1.0):
    batch_size, latent_dim = z.shape
    z_fake = torch.randn(batch_size, latent_dim).cuda() * sigma
    #z_fake = norm(z_fake)
    mmd_loss = -imq_kernel(z, z_fake, h_dim=latent_dim)
    return mmd_loss.mean()


# Normalize a batch of latent points to the unit hypersphere
def norm(x):
    norm = torch.norm(x, p=2, dim=1)
    x = x / (norm.expand(1, -1).t() + .0001)
    return x


def main():
    batch_size = 64
    latent_dim = 16
    true_latent_dim = 4
    num_actions = 4
    train_iters = 100 * 1000
    encoder = models.Encoder(latent_dim)
    decoder = models.Decoder(latent_dim)
    discriminator = models.Discriminator()
    transition = models.Transition(latent_dim, num_actions)
    blur = models.GaussianSmoothing(channels=3, kernel_size=11, sigma=4.)
    higgins_scores = []

    #load_from_dir = '.'
    load_from_dir = '/mnt/nfs/experiments/default/scm-gan_e82c2d10'
    if load_from_dir is not None and 'model-encoder.pth' in os.listdir(load_from_dir):
        print('Loading models from directory {}'.format(load_from_dir))
        encoder.load_state_dict(torch.load(os.path.join(load_from_dir, 'model-encoder.pth')))
        decoder.load_state_dict(torch.load(os.path.join(load_from_dir, 'model-decoder.pth')))
        transition.load_state_dict(torch.load(os.path.join(load_from_dir, 'model-transition.pth')))
        discriminator.load_state_dict(torch.load(os.path.join(load_from_dir, 'model-discriminator.pth')))

    # Train the autoencoder
    opt_enc = torch.optim.Adam(encoder.parameters(), lr=.001)
    opt_dec = torch.optim.Adam(decoder.parameters(), lr=.001)
    opt_trans = torch.optim.Adam(transition.parameters(), lr=.001)
    opt_disc = torch.optim.Adam(discriminator.parameters(), lr=.0005)
    ts = TimeSeries('Training Model', train_iters)
    for train_iter in range(0, train_iters):
        theta = (train_iter / train_iters)
        timesteps = 5 + int(20 * theta)
        encoder.train()
        decoder.train()
        transition.train()
        discriminator.train()
        for model in (encoder, decoder, transition, discriminator):
            for child in model.children():
                if type(child) == nn.BatchNorm2d or type(child) == nn.BatchNorm1d:
                    child.momentum = 0.1

        # Train discriminator
        """
        states, rewards, dones, actions = datasource.get_trajectories(batch_size, 1)
        states = torch.Tensor(states[:, 0]).cuda()
        opt_disc.zero_grad()
        real_scores = discriminator(states)
        fake_scores = discriminator(decoder(encoder(states)))
        real_loss = torch.mean(F.relu(1 - real_scores))
        fake_loss = torch.mean(F.relu(1 + fake_scores))
        ts.collect('D. real', real_loss)
        ts.collect('D. fake', fake_loss)
        disc_loss = real_loss + fake_loss
        disc_loss.backward()
        opt_disc.step()
        """

        # Train the rest of the network
        opt_enc.zero_grad()
        opt_dec.zero_grad()
        opt_trans.zero_grad()

        # Train decoder using discriminator
        states, rewards, dones, actions = datasource.get_trajectories(batch_size, 1)
        states = torch.Tensor(states[:, 0]).cuda()

        """
        fake_scores = discriminator(decoder(encoder(states).detach()))
        gen_loss = .0001 * theta * torch.mean(F.relu(1 - fake_scores))
        ts.collect('D. gen', gen_loss)
        gen_loss.backward()
        """

        states, rewards, dones, actions = datasource.get_trajectories(batch_size, timesteps)
        states = torch.Tensor(states).cuda()
        # states.shape: (batch_size, timesteps, 3, 64, 64)

        # Predict the output of the game
        loss = 0
        z = encoder(states[:, 0])
        ts.collect('encoder z[0] mean', z[0].mean())
        for t in range(timesteps):
            predicted = decoder(z)

            l1_penalty = theta * .01 * z.abs().mean()
            ts.collect('L1 t={}'.format(t), l1_penalty)
            loss += l1_penalty

            expected = states[:, t]

            # MSE loss
            rec_loss = torch.mean((expected - predicted)**2)
            # MSE loss but blurred to prevent pathological behavior
            #rec_loss = torch.mean((blur(expected) - blur(predicted))**2)
            # MSE loss but weighted toward foreground pixels
            #error_mask = torch.mean((expected - predicted) ** 2, dim=1)
            #foreground_mask = torch.mean(blur(expected), dim=1)
            #error_mask = theta * error_mask + (1 - theta) * (error_mask * foreground_mask)
            #rec_loss = torch.mean(error_mask)

            ts.collect('Recon. t={}'.format(t), rec_loss)
            loss += rec_loss

            # Latent regression loss: Don't encode non-visible information
            #z_prime = encoder(decoder(z))
            #latent_regression_loss = torch.mean((z - z_prime)**2)
            #ts.collect('Latent reg. t={}'.format(t), latent_regression_loss)
            #loss += latent_regression_loss

            # Predict the next latent point
            onehot_a = torch.eye(num_actions)[actions[:, t]].cuda()
            new_z = transition(z, onehot_a)

            trans_l1_penalty = theta * .01 * (new_z - z).abs().mean()
            ts.collect('T-L1 t={}'.format(t), trans_l1_penalty)
            loss += trans_l1_penalty

            z = new_z

            # Maximum Mean Discrepancy: Regularization toward gaussian
            # mmd_loss = mmd_normal_penalty(z)
            # ts.collect('MMD Loss t={}'.format(t), mmd_loss)
            # loss += mmd_loss

        loss.backward()

        opt_enc.step()
        opt_dec.step()
        opt_trans.step()
        ts.print_every(2)

        encoder.eval()
        decoder.eval()
        transition.eval()
        discriminator.eval()
        for model in (encoder, decoder, transition, discriminator):
            for child in model.children():
                if type(child) == nn.BatchNorm2d or type(child) == nn.BatchNorm1d:
                    child.momentum = 0

        compute_causal_graph(encoder, transition, states, actions, latent_dim=latent_dim, num_actions=num_actions)

        if train_iter % 100 == 0:
            vis = ((expected - predicted)**2)[:1]
            imutil.show(vis, filename='reconstruction_error.png')

        if train_iter % 100 == 0:
            visualize_reconstruction(encoder, decoder, states, train_iter=train_iter)

        # Periodically generate latent space traversals
        if train_iter % 1000 == 0:
            visualize_latent_space(states, encoder, decoder, latent_dim=latent_dim, train_iter=train_iter)

        # Periodically save the network
        if train_iter % 1000 == 0:
            print('Saving networks to filesystem...')
            torch.save(transition.state_dict(), 'model-transition.pth')
            torch.save(encoder.state_dict(), 'model-encoder.pth')
            torch.save(decoder.state_dict(), 'model-decoder.pth')
            torch.save(discriminator.state_dict(), 'model-discriminator.pth')

        # Periodically generate simulations of the future
        if train_iter % 1000 == 0:
            visualize_forward_simulation(datasource, encoder, decoder, transition, train_iter)

        # Periodically compute the Higgins score
        """
        if train_iter % 10000 == 0:
            if not hasattr(datasource, 'simulator'):
                print('Datasource {} does not support direct simulation, skipping disentanglement metrics'.format(datasource.__name__))
            else:
                trained_score = higgins_metric(datasource.simulator, true_latent_dim, encoder, latent_dim)
                higgins_scores.append(trained_score)
                print('Higgins metric before training: {}'.format(higgins_scores[0]))
                print('Higgins metric after training {} iters: {}'.format(train_iter, higgins_scores[-1]))
                print('Best Higgins: {}'.format(max(higgins_scores)))
                ts.collect('Higgins Metric', trained_score)
        """
    print(ts)
    print('Finished')


def compute_causal_graph(encoder, transition, states, actions, latent_dim, num_actions):
    # Generate a z and new_z by playing out two time steps

    # Start with latent point t=0 (note: t=0 is a special case)
    # Note: z_{t=0} is a special case so we use t=1 vs. t=2
    z = encoder(states[:, 0])

    # Compute z at t=1 and t=2
    onehot_a = torch.eye(num_actions)[actions[:, 0]].cuda()
    src_z = transition(z, onehot_a)
    onehot_a = torch.eye(num_actions)[actions[:, 1]].cuda()
    dst_z = transition(src_z, onehot_a)

    # Edge weights for the causal graph: close-to-zero weights can be pruned
    causal_edge_weights = np.zeros(shape=(latent_dim, latent_dim))

    # For each latent factor, check which other factors it "causes"
    # by computing a counterfactual s_{t+1}
    print("Generating counterfactual perturbations for latent factors dim {}".format(latent_dim))
    for src_factor_idx in range(latent_dim):
        # The next timestep (according to our model)
        ground_truth_outcome = dst_z

        # What if z[:,latent_idx] had been erased, set to zero?
        perturbed_src_z = src_z.clone()
        perturbed_src_z[:, src_factor_idx] = 0

        # The counterfactual next timestep (according to our model)
        counterfactual_outcome = transition(perturbed_src_z, onehot_a)

        # Difference between what we normally expect to happen,
        #  and what *would* happen IF NOT FOR the source factor
        cf_difference = (ground_truth_outcome - counterfactual_outcome)**2
        for dst_factor_idx in range(latent_dim):
            edge_weight = cf_difference[:,dst_factor_idx].sum()
            print("Factor {} -> Factor {} causal strength: {:.04f}".format(
                src_factor_idx, dst_factor_idx, edge_weight))
            causal_edge_weights[src_factor_idx, dst_factor_idx] = edge_weight
    print("Finished generating counterfactual perturbations")

    print("Normalizing counterfactual perturbations to max {}".format(causal_edge_weights.max()))
    causal_edge_weights /= causal_edge_weights.max()

    print('Causal Graph Edge Weights')
    print('Latent Factor -> Latent Factor dim={}'.format(latent_dim))
    for i in range(causal_edge_weights.shape[0]):
        for j in range(causal_edge_weights.shape[1]):
            print('{:.03f}\t'.format(causal_edge_weights[i,j]), end='')
        print('')
    graph_img = render_causal_graph(causal_edge_weights)
    import pdb; pdb.set_trace()
    imutil.show(graph_img, filename='causal_graph.png')


def visualize_reconstruction(encoder, decoder, states, train_iter=0):
    # Image of reconstruction
    filename = 'vis_iter_{:06d}.png'.format(train_iter)
    ground_truth = states[:, 0]
    tag = 'iter_{:06d}'.format(train_iter)
    logits = decoder(encoder(ground_truth))
    #reconstructed = torch.sigmoid(logits)
    reconstructed = logits
    img = torch.cat((ground_truth[:4], reconstructed[:4]), dim=3)
    caption = 'D(E(x)) iter {}'.format(train_iter)
    imutil.show(img, resize_to=(640, 360), img_padding=4,
                filename='visual_reconstruction_{}.png'.format(tag),
                caption=caption, font_size=10)


def visualize_latent_space(states, encoder, decoder, latent_dim, train_iter=0, frames=120, img_size=800):
    # Create a "batch" containing copies of the same image, one per latent dimension
    ground_truth = states[:, 0]

    for i in range(1, latent_dim):
        ground_truth[i] = ground_truth[0]
    zt = encoder(ground_truth)
    zt.detach()
    #minval, maxval = decoder.to_categorical.rho.min(), decoder.to_categorical.rho.max()
    minval, maxval = -1, 1

    # Generate L videos, one per latent dimension
    vid = imutil.Video('latent_traversal_dims_{:04d}_iter_{:06d}'.format(latent_dim, train_iter))
    for frame_idx in range(frames):
        for z_idx in range(latent_dim):
            z_val = (frame_idx / frames) * (maxval - minval) + minval
            zt[z_idx, z_idx] = z_val
        #output = torch.sigmoid(decoder(zt))
        output = decoder(zt)[:latent_dim]
        #reconstructed = torch.sigmoid(decoder(encoder(ground_truth)))
        reconstructed = decoder(encoder(ground_truth))
        video_frame = torch.cat([ground_truth[:1], reconstructed[:1], output], dim=0)
        caption = '{}/{} z range [{:.02f} {:.02f}]'.format(frame_idx, frames, minval, maxval)
        # Clip and scale
        video_frame = 255 * torch.clamp(video_frame, 0, 1)
        vid.write_frame(video_frame, resize_to=(img_size,img_size), caption=caption, img_padding=8, normalize=False)
    vid.finish()


def visualize_forward_simulation(datasource, encoder, decoder, transition, train_iter=0, timesteps=60, num_actions=4):
    start_time = time.time()
    print('Starting trajectory simulation for {} frames'.format(timesteps))
    states, rewards, dones, actions = datasource.get_trajectories(batch_size=64, timesteps=timesteps)
    states = torch.Tensor(states).cuda()
    vid_simulation = imutil.Video('simulation_only_iter_{:06d}.mp4'.format(train_iter), framerate=3)
    vid_features = imutil.Video('simulation_iter_{:06d}.mp4'.format(train_iter), framerate=3)
    vid_separable_conv = imutil.Video('simulation_separable_iter_{:06d}.mp4'.format(train_iter), framerate=3)
    z = encoder(states[:, 0])
    z.detach()
    for t in range(timesteps - 1):
        x_t, x_t_separable = decoder(z, visualize=True)

        # Render top row: real video vs. simulation from initial conditions
        pixel_view = torch.cat((states[:, t][:1], x_t[:1]), dim=3)
        caption = 'Pred. t+{} a={} min={:.2f} max={:.2f}'.format(t, actions[:1, t], pixel_view.min(), pixel_view.max())
        top_row = imutil.show(pixel_view.clamp_(0,1), caption=caption, img_padding=8, font_size=10, resize_to=(800,400), return_pixels=True, display=False, save=False)
        caption = 'Left: Real          Right: Simulated from initial conditions t={}'.format(t)
        vid_simulation.write_frame(pixel_view.clamp(0, 1), caption=caption, resize_to=(1280,640))

        # Render latent representation of simulation
        bottom_row = imutil.show(z[0], resize_to=(800,800), return_pixels=True, img_padding=8, display=False, save=False)
        vid_features.write_frame(np.concatenate([top_row, bottom_row], axis=0))

        # Render pixels generated from latent representation (groupwise separable)
        separable_output = imutil.show(x_t_separable, resize_to=(800,800), return_pixels=True, img_padding=8, display=False, save=False)
        vid_separable_conv.write_frame(np.concatenate([top_row, separable_output], axis=0))

        # Predict the next latent point
        onehot_a = torch.eye(num_actions)[actions[:, t + 1]].cuda()
        z = transition(z, onehot_a).detach()

    vid_simulation.finish()
    vid_features.finish()
    vid_separable_conv.finish()
    print('Finished trajectory simulation in {:.02f}s'.format(time.time() - start_time))


if __name__ == '__main__':
    main()
