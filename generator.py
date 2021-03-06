import os
import numpy as np
import torch
import torch.nn as nn
from torchvision.models import vgg19
import matplotlib.pyplot as plt
from scipy.stats import ortho_group

from utils import output_value_hook, sliced_transport, image_preprocessing, vgg_normalization


class Generator:

    def __init__(self, image, observed_layers, n_bins=128):

        # source image
        self.source_tensor = image_preprocessing(image)
        self.normalized_source_batch = vgg_normalization(
            self.source_tensor).unsqueeze(0)
        self.source_batch = self.source_tensor.unsqueeze(0)

        # set encoder
        self.encoder = vgg19(pretrained=True).float()
        self.encoder.eval()
        for param in self.encoder.features.parameters():
            param.requires_grad = False
        self.encoder_layers = {}
        self.set_encoder_hooks(observed_layers)

        self.n_bins = n_bins

    def set_encoder_hooks(self, observed_layers):

        self.observed_layers = observed_layers
        self.error_values = {layer_name: []
                             for layer_name in observed_layers.keys()}
        self.decoder_loss_values = {}

        for layer_name, layer_information in observed_layers.items():

            # set a hook in the encoder
            layer_index = layer_information['index']
            layer = self.encoder.features[layer_index]
            layer.register_forward_hook(
                output_value_hook(layer_name, self.encoder_layers))

    def set_layer_decoders(self, train=False, state_dir_path='decoder_states'):

        # train
        if train:
            for layer_name, layer_information in self.observed_layers.items():
                decoder = layer_information['decoder']

                print(f'training decoder for layer {layer_name}')
                loss_values = self.train_decoder(layer_name)
                self.decoder_loss_values[layer_name] = loss_values

        # load or save weights
        for layer_name, layer_information in self.observed_layers.items():
            decoder = layer_information['decoder']
            decoder.eval()
            decoder_state_path = os.path.join(
                state_dir_path, f'{layer_name}_decoder_state.pth')

            # save the tained weights
            if train:
                torch.save(decoder.state_dict(), decoder_state_path)
                print(f'saved decoder weights for layer {layer_name}')

            else:
                decoder.load_state_dict(torch.load(decoder_state_path))
                print(f'loaded decoder weights for layer {layer_name}')
                decoder = decoder.float()

    def generate(self, n_passes=5):

        self.n_passes = n_passes
        pass_generated_images = []

        # initialize with noise
        self.target_tensor = torch.randn_like(self.source_tensor)

        for global_pass in range(n_passes):
            print(f'global pass {global_pass}')
            for layer_name, layer_information in self.observed_layers.items():
                print(f'layer {layer_name}')

                # forward pass on source image
                self.encoder(self.normalized_source_batch)
                source_layer = self.encoder_layers[layer_name]

                # forward pass on target image
                target_batch = vgg_normalization(
                    self.target_tensor).unsqueeze(0)
                self.encoder(target_batch)
                target_layer = self.encoder_layers[layer_name]

                # transport
                target_layer = self.optimal_transport(layer_name,
                                                      source_layer.squeeze(), target_layer.squeeze())
                target_layer = target_layer.view_as(source_layer)

                # decode
                decoder = layer_information['decoder']
                self.target_tensor = decoder(target_layer).squeeze()

                generated_image = np.transpose(
                    self.target_tensor.numpy(), (1, 2, 0)).copy()
                pass_generated_images.append(generated_image)

        return pass_generated_images

    def optimal_transport(self, layer_name, source_layer, target_layer):

        n_channels = source_layer.shape[0]
        assert n_channels == target_layer.shape[0]

        default_n_slices = n_channels // self.n_passes
        n_slices = self.observed_layers[layer_name].get(
            'n_slices', default_n_slices)

        for slice in range(n_slices):
            # random orthonormal basis
            basis = torch.from_numpy(ortho_group.rvs(n_channels)).float()

            # project on the basis
            source_rotated_layer = basis @ source_layer.view(
                n_channels, -1)
            target_rotated_layer = basis @ target_layer.view(
                n_channels, -1)

            # sliced transport
            target_rotated_layer = sliced_transport(
                source_rotated_layer, target_rotated_layer)
            target_layer = basis.t() @ target_rotated_layer

        return target_layer

    def reconstruct(self, noise_size=0):

        reconstructed_images = []

        for layer_name, layer_information in self.observed_layers.items():
            print(f'layer {layer_name}')

            self.encoder(self.normalized_source_batch)
            source_layer = self.encoder_layers[layer_name]

            source_layer += noise_size * torch.randn_like(source_layer)

            decoder = layer_information['decoder']
            input_reconstruction = np.transpose(
                decoder(source_layer), (1, 2, 0)).copy()
            reconstructed_images.append(input_reconstruction)

        return reconstructed_images

    def train_decoder(self, layer_name):
        decoder = self.observed_layers[layer_name]['decoder']
        n_epochs = self.observed_layers[layer_name]['n_epochs']
        learning_rate = self.observed_layers[layer_name].get(
            'learning_rate', 1e-3)
        training_loss_values = []
        image_loss = nn.MSELoss()
        optimizer = torch.optim.Adam(decoder.parameters(), lr=learning_rate)
        for epoch_index in range(n_epochs):
            #print(f'Epoch {epoch_index}')
            epoch_loss = 0

            # reconstruct
            self.encoder(vgg_normalization(self.source_tensor).unsqueeze(0))
            embedding = self.encoder_layers[layer_name]
            generated_tensor = decoder(embedding).squeeze()

            # re-embed
            self.encoder(vgg_normalization(generated_tensor).unsqueeze(0))
            generated_embedding = self.encoder_layers[layer_name]

            loss = image_loss(self.source_tensor, generated_tensor) + \
                torch.norm(embedding - generated_embedding)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_loss += loss

            training_loss_values.append(epoch_loss)
        return training_loss_values
