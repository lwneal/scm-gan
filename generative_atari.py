import time
import os
import json
import numpy as np
import random

from tqdm import tqdm
import imutil
from logutil import TimeSeries

from atari import MultiEnvironment
from causal_graph import compute_causal_graph, render_causal_graph
from skimage.measure import block_reduce

latent_size = 6
l1_power = 1.0
disc_power = .001
num_actions = 6
batch_size = 32
iters = 100 * 1000

env = None
prev_states = None


def get_batch():
    global env
    global prev_states
    if env is None:
        env = MultiEnvironment('PongDeterministic-v4', batch_size)
        actions = np.random.randint(0, num_actions, size=batch_size)
        prev_states, rewards, dones, infos = env.step(actions)
        prev_states = torch.Tensor(prev_states).cuda()

    actions = np.random.randint(0, num_actions, size=batch_size)
    states, rewards, dones, infos = env.step(actions)

    states = torch.Tensor(states).cuda()
    action_tensor = torch.Tensor(batch_size, num_actions).zero_().cuda()
    for i in range(batch_size):
        action_tensor[i][actions[i]] = 1.0
    results = (prev_states, action_tensor, states)
    prev_states = states
    return results


# ok now we build a neural network
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

class Encoder(nn.Module):
    def __init__(self, latent_size):
        super().__init__()
        self.latent_size = latent_size
        # 1x32x32
        self.conv1 = nn.Conv2d(3, 32, 3, stride=1, padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        # 32x32x32
        self.conv2 = nn.Conv2d(32, 64, 4, stride=2, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        # 64x16x16
        self.conv3 = nn.Conv2d(64, 64, 4, stride=2, padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        # 64x8x8
        self.conv4 = nn.Conv2d(64, 64, 4, stride=2, padding=1)
        self.bn4 = nn.BatchNorm2d(64)
        # 64x4x4
        self.fc1 = nn.Linear(64*4*4, 128)
        self.bn5 = nn.BatchNorm1d(128)
        self.fc2 = nn.Linear(128, latent_size)
        self.cuda()

    def forward(self, x):
        # Input: batch x 32 x 32
        #x = x.unsqueeze(1)
        # batch x 1 x 32 x 32
        x = self.conv1(x)
        x = self.bn1(x)
        x = F.leaky_relu(x, 0.2)

        x = self.conv2(x)
        x = self.bn2(x)
        x = F.leaky_relu(x, 0.2)

        x = self.conv3(x)
        x = self.bn3(x)
        x = F.leaky_relu(x, 0.2)

        x = self.conv4(x)
        x = self.bn4(x)
        x = F.leaky_relu(x, 0.2)

        x = x.view(-1, 64*4*4)
        x = self.fc1(x)
        x = self.bn5(x)

        z = self.fc2(x)
        return z


class Discriminator(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1a = nn.Linear(32, 128)
        self.fc1b = nn.Linear(32, 128)
        self.fc2 = nn.Linear(256, 1)
        self.cuda()

    def forward(self, x):
        # A constrained discriminator
        # It can see only projections, 1D shadows of the 2D input

        # Input: batch x 3 x 32 x 32
        x = x.mean(dim=1)
        latitude = self.fc1a(x.mean(dim=1))
        longitude = self.fc1a(x.mean(dim=2))
        x = torch.cat([latitude, longitude], dim=1)
        x = F.leaky_relu(x, 0.2)
        x = self.fc2(x)
        return x


class Decoder(nn.Module):
    def __init__(self, latent_size):
        super().__init__()
        self.latent_size = latent_size
        self.fc1 = nn.Linear(latent_size, 128)
        self.bn0 = nn.BatchNorm1d(128)

        self.fc2 = nn.Linear(128, 256)

        self.deconv1 = nn.ConvTranspose2d(256, 128, 4, stride=1)
        self.bn1 = nn.BatchNorm2d(128)
        # 128 x 4 x 4
        self.deconv2 = nn.ConvTranspose2d(128, 64, 4, stride=2, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        # 64 x 8 x 8
        self.deconv3 = nn.ConvTranspose2d(64, 64, 4, stride=2, padding=1)
        self.bn3 = nn.BatchNorm2d(64)
        # 64 x 16 x 16
        self.deconv4 = nn.ConvTranspose2d(64, 64, 4, stride=2, padding=1)
        self.bn4 = nn.BatchNorm2d(64)
        # 64 x 32 x 32
        self.deconv5 = nn.ConvTranspose2d(64, 3, 3, stride=1, padding=1)
        self.cuda()

    def forward(self, z):
        x = self.fc1(z)
        x = F.leaky_relu(x, 0.2)
        x = self.bn0(x)

        x = self.fc2(x)
        x = F.leaky_relu(x, 0.2)

        x = x.unsqueeze(-1).unsqueeze(-1)
        x = self.deconv1(x)
        x = F.leaky_relu(x, 0.2)
        x = self.bn1(x)

        x = self.deconv2(x)
        x = F.leaky_relu(x, 0.2)
        x = self.bn2(x)

        x = self.deconv3(x)
        x = F.leaky_relu(x, 0.2)
        x = self.bn3(x)

        x = self.deconv4(x)
        x = F.leaky_relu(x, 0.2)
        x = self.bn4(x)

        x = self.deconv5(x)
        x = torch.sigmoid(x)
        return x


class Transition(nn.Module):
    def __init__(self, latent_size, num_actions):
        super().__init__()
        # Input: State + Action
        # Output: State
        self.fc1 = nn.Linear(latent_size + num_actions, latent_size*2)
        self.fc2 = nn.Linear(latent_size*2, latent_size, bias=False)
        # one-layer version
        #self.fc1 = nn.Linear(5, 4)
        self.cuda()

    def forward(self, z, actions):
        x = torch.cat([z, actions], dim=1)

        x = self.fc1(x)
        x = F.leaky_relu(x, 0.2)
        x = self.fc2(x)
        # Fern hack: Predict a delta/displacement
        return z + x



def demo_latent_video(before, encoder, decoder, transition, latent_size, num_actions, epoch=0):
    start_time = time.time()
    batch_size = before.shape[0]
    actions = torch.zeros(batch_size, num_actions).cuda()
    for i in range(batch_size):
        a = np.random.randint(0, num_actions)
        actions[i, a] = 1

    prev_z = encoder(before)
    #z = transition(prev_z, actions)
    z = prev_z
    for i in range(latent_size):
        vid_filename = 'iter_{:06d}_dim_{:02d}'.format(epoch, i)
        vid = imutil.VideoLoop(vid_filename)
        dim_min = z[:,i].min().item()
        dim_max = z[:,i].max().item()
        N = 60
        for j in range(N):
            dim_range = dim_max - dim_min
            val = dim_min + dim_range * 1.0 * j / N
            zp = z.clone()
            zp[:, i] = val
            caption = "z{}={:.3f}".format(i, val)
            img = decoder(zp)
            vid.write_frame(img, caption=caption, resize_to=(256,256), img_padding=10)
        vid.finish()
    print('Finished generating videos in {:03f}s'.format(time.time() - start_time))


def demo_interpolation_video(before, encoder, decoder, transition, latent_size, num_actions, epoch=0):
    start_time = time.time()

    # Take the first two images and interpolate between them
    z = encoder(before[:2])
    start_z = z[0:1]
    end_z = z[1:2]

    vid_filename = 'iter_{:06d}_interp'.format(epoch)
    vid = imutil.VideoLoop(vid_filename)
    for i in range(latent_size):
        N = 60
        for j in range(N):
            val = 1.0 * j / N
            zp = val * end_z + (1 - val) * start_z
            img = decoder(zp)
            caption = "Interp {:.3f}".format(val)
            vid.write_frame(img, caption=caption, resize_to=(256,256))
    vid.finish()
    print('Finished generating interpolation in {:03f}s'.format(time.time() - start_time))


def clip_gradients(network, val):
    for W in network.parameters():
        if W.grad is not None:
            W.grad.clamp_(-val, val)


def clip_weights(network, val):
    for W in network.parameters():
        if W.data is not None:
            W.data.clamp_(-val, val)


def main():
    # ok now, can the network learn the task?
    encoder = Encoder(latent_size)
    decoder = Decoder(latent_size)
    discriminator = Discriminator()
    transition = Transition(latent_size, num_actions)
    opt_encoder = optim.Adam(encoder.parameters(), lr=0.0001)
    opt_decoder = optim.Adam(decoder.parameters(), lr=0.0001)
    opt_transition = optim.Adam(transition.parameters(), lr=0.0001)
    opt_discriminator = optim.Adam(discriminator.parameters(), lr=0.0001)

    ts = TimeSeries('Training', iters)

    vid = imutil.VideoMaker('causal_model.mp4')
    for i in range(iters):
        encoder.train()
        decoder.train()
        discriminator.train()
        transition.train()
        # First train the discriminator
        for j in range(3):
            opt_discriminator.zero_grad()
            _, _, real = get_batch()
            fake = decoder(encoder(real))
            disc_real = torch.relu(1 + discriminator(real)).sum()
            disc_fake = torch.relu(1 - discriminator(fake)).sum()
            disc_loss = disc_real + disc_fake
            disc_loss.backward()
            #clip_gradients(discriminator, 1)
            opt_discriminator.step()
        pixel_variance = fake.var(0).mean()
        ts.collect('Disc real loss', disc_real)
        ts.collect('Disc fake loss', disc_fake)
        ts.collect('Discriminator loss', disc_loss)
        ts.collect('Generated pixel variance', pixel_variance)
        clip_weights(discriminator, .01)

        # Apply discriminator loss for realism
        opt_decoder.zero_grad()
        _, _, real = get_batch()
        fake = decoder(encoder(real))
        disc_loss = disc_power * torch.relu(1 + discriminator(fake)).sum()
        ts.collect('Gen. Disc loss', disc_loss)
        disc_loss.backward()
        #clip_gradients(decoder, .01)
        opt_decoder.step()

        # Now train the autoencoder
        opt_encoder.zero_grad()
        opt_decoder.zero_grad()
        opt_transition.zero_grad()

        before, actions, target = get_batch()

        z = encoder(before)
        z_prime = transition(z, actions)
        predicted = decoder(z_prime)

        #pred_loss = F.binary_cross_entropy(predicted, target)
        pred_loss = torch.mean((predicted - target) ** 2)
        ts.collect('Reconstruction loss', pred_loss)

        l1_scale = (l1_power * i) / iters
        l1_loss = 0.
        l1_loss += l1_scale * F.l1_loss(transition.fc1.bias, torch.zeros(transition.fc1.bias.shape).cuda())
        l1_loss += l1_scale * F.l1_loss(transition.fc1.weight, torch.zeros(transition.fc1.weight.shape).cuda())
        l1_loss += l1_scale * F.l1_loss(transition.fc2.weight, torch.zeros(transition.fc2.weight.shape).cuda())
        ts.collect('Sparsity loss', l1_loss)

        loss = pred_loss + l1_loss

        loss.backward()
        opt_encoder.step()
        opt_decoder.step()
        opt_transition.step()

        encoder.eval()
        decoder.eval()
        discriminator.eval()
        transition.eval()

        if i % 100 == 0:
            filename = 'iter_{:06}_reconstruction.jpg'.format(i)
            img = torch.cat([target[:2], predicted[:2]])
            caption = 'iter {}: top orig. bot recon.'.format(i)
            imutil.show(img, filename=filename, resize_to=(256,256), img_padding=10, caption=caption)

            scm = compute_causal_graph(transition, latent_size, num_actions)
            caption = 'Iteration {} Prediction Loss {:.03f}'.format(i, pred_loss)
            vid.write_frame(render_causal_graph(scm), caption=caption)

        if i % 1000 == 0:
            demo_latent_video(before[:9], encoder, decoder, transition, latent_size, num_actions, epoch=i)
            demo_interpolation_video(before[:2], encoder, decoder, transition, latent_size, num_actions, epoch=i)
        ts.print_every(2)


    vid.finish()
    print(ts)


if __name__ == '__main__':
    main()
