import os
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.utils.data import DataLoader
import matplotlib.pyplot as plt

# -----------------------------
# Data Transforms and Dataset
# -----------------------------
img_size = 64
transform_train = transforms.Compose([
    transforms.Resize((img_size, img_size)),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
])
transform_test = transforms.Compose([
    transforms.Resize((img_size, img_size)),
    transforms.ToTensor(),
])

data_root = "./data"
train_dataset = datasets.Flowers102(root=data_root, split='train', download=True, transform=transform_train)
test_dataset  = datasets.Flowers102(root=data_root, split='test', download=True, transform=transform_test)
batch_size = 64
train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=2, pin_memory=True)
test_loader  = DataLoader(test_dataset, batch_size=batch_size, shuffle=False, num_workers=2, pin_memory=True)

# -----------------------------
# Simple UNet for Pixel Diffusion
# -----------------------------
class SimpleUNet(nn.Module):
    def __init__(self, in_channels=3, base_channels=64, time_emb_dim=128):
        super().__init__()
        self.time_emb_dim = time_emb_dim
        # Time embedding: map scalar timestep to a vector
        self.time_embed = nn.Sequential(
            nn.Linear(1, time_emb_dim),
            nn.ReLU(),
            nn.Linear(time_emb_dim, time_emb_dim),
        )
        # Encoder
        self.conv1 = nn.Sequential(
            nn.Conv2d(in_channels, base_channels, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv2d(base_channels, base_channels, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        self.down1 = nn.Conv2d(base_channels, base_channels * 2, kernel_size=4, stride=2, padding=1)  # 64 -> 32
        self.conv2 = nn.Sequential(
            nn.Conv2d(base_channels * 2, base_channels * 2, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv2d(base_channels * 2, base_channels * 2, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        self.down2 = nn.Conv2d(base_channels * 2, base_channels * 4, kernel_size=4, stride=2, padding=1)  # 32 -> 16
        self.conv3 = nn.Sequential(
            nn.Conv2d(base_channels * 4, base_channels * 4, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv2d(base_channels * 4, base_channels * 4, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        # Bottleneck
        self.bottleneck = nn.Sequential(
            nn.Conv2d(base_channels * 4, base_channels * 8, kernel_size=3, padding=1),
            nn.ReLU(),
            nn.Conv2d(base_channels * 8, base_channels * 4, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        # Decoder
        self.up1 = nn.ConvTranspose2d(base_channels * 4, base_channels * 2, kernel_size=4, stride=2, padding=1)  # 16 -> 32
        self.conv4 = nn.Sequential(
            nn.Conv2d(base_channels * 4, base_channels * 2, kernel_size=3, padding=1),  # concat skip from conv2
            nn.ReLU(),
            nn.Conv2d(base_channels * 2, base_channels * 2, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        self.up2 = nn.ConvTranspose2d(base_channels * 2, base_channels, kernel_size=4, stride=2, padding=1)  # 32 -> 64
        self.conv5 = nn.Sequential(
            nn.Conv2d(base_channels * 2, base_channels, kernel_size=3, padding=1),  # concat skip from conv1
            nn.ReLU(),
            nn.Conv2d(base_channels, base_channels, kernel_size=3, padding=1),
            nn.ReLU(),
        )
        self.out_conv = nn.Conv2d(base_channels, in_channels, kernel_size=3, padding=1)
    
    def forward(self, x, t):
        """
        x: noisy image tensor of shape (B, 3, 64, 64)
        t: timestep tensor of shape (B,) or (B,1)
        """
        B = x.size(0)
        t = t.view(B, 1).float()
        t_emb = self.time_embed(t)  # shape (B, time_emb_dim)
        # Encoder
        x1 = self.conv1(x)  # (B, base_channels, 64, 64)
        # Add time conditioning (use the first channels of t_emb)
        x1 = x1 + t_emb[:, :x1.shape[1]].view(B, x1.shape[1], 1, 1)
        x2 = self.down1(x1)  # (B, base_channels*2, 32, 32)
        x2 = self.conv2(x2)  # (B, base_channels*2, 32, 32)
        x2 = x2 + t_emb[:, :x2.shape[1]].view(B, x2.shape[1], 1, 1)
        x3 = self.down2(x2)  # (B, base_channels*4, 16, 16)
        x3 = self.conv3(x3)  # (B, base_channels*4, 16, 16)
        x3 = x3 + t_emb[:, :x3.shape[1]].view(B, x3.shape[1], 1, 1)
        # Bottleneck
        x4 = self.bottleneck(x3)  # (B, base_channels*4, 16, 16)
        # Decoder
        x5 = self.up1(x4)  # (B, base_channels*2, 32, 32)
        x5 = torch.cat([x5, x2], dim=1)  # concatenate skip from encoder
        x5 = self.conv4(x5)  # (B, base_channels*2, 32, 32)
        x6 = self.up2(x5)    # (B, base_channels, 64, 64)
        x6 = torch.cat([x6, x1], dim=1)  # concatenate skip from encoder
        x6 = self.conv5(x6)  # (B, base_channels, 64, 64)
        out = self.out_conv(x6)  # (B, 3, 64, 64)
        return out

# -----------------------------
# Diffusion Process in Pixel Space
# -----------------------------
class DiffusionModel:
    def __init__(self, model, n_steps=1000, beta_start=0.0001, beta_end=0.02, device="cpu"):
        self.model = model
        self.n_steps = n_steps
        self.device = device
        self.beta = torch.linspace(beta_start, beta_end, n_steps).to(device)
        self.alpha = 1 - self.beta
        self.alpha_bar = torch.cumprod(self.alpha, dim=0)

    def q_sample(self, x0, t, noise=None):
        if noise is None:
            noise = torch.randn_like(x0)
        # t is a tensor of indices, reshape alpha_bar accordingly
        alpha_bar_t = self.alpha_bar[t].view(-1, 1, 1, 1)
        return torch.sqrt(alpha_bar_t) * x0 + torch.sqrt(1 - alpha_bar_t) * noise

    def p_sample(self, xt, t):
        B = xt.size(0)
        t_tensor = torch.full((B,), t, device=self.device, dtype=torch.long)
        eps_pred = self.model(xt, t_tensor)
        alpha_t = self.alpha[t]
        alpha_bar_t = self.alpha_bar[t]
        mean = (xt - ((1 - alpha_t) / torch.sqrt(1 - alpha_bar_t)) * eps_pred) / torch.sqrt(alpha_t)
        if t > 0:
            noise = torch.randn_like(xt)
            var = self.beta[t]
            sample = mean + torch.sqrt(var) * noise
        else:
            sample = mean
        return sample

    def sample(self, shape):
        # Start from pure noise
        x = torch.randn(shape, device=self.device)
        for t in reversed(range(self.n_steps)):
            x = self.p_sample(x, t)
        return x

    def loss(self, x0):
        B = x0.size(0)
        t = torch.randint(0, self.n_steps, (B,), device=self.device).long()
        noise = torch.randn_like(x0)
        x_t = self.q_sample(x0, t, noise)
        noise_pred = self.model(x_t, t)
        return F.mse_loss(noise_pred, noise)

# -----------------------------
# Training Function for Diffusion Model
# -----------------------------
def train_diffusion(diffusion, dataloader, num_epochs, device):
    optimizer = optim.Adam(diffusion.model.parameters(), lr=1e-4)
    diffusion.model.train()
    for epoch in range(num_epochs):
        total_loss = 0.0
        for images, _ in dataloader:
            images = images.to(device)
            loss = diffusion.loss(images)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        print(f"Diffusion Epoch {epoch+1}/{num_epochs}, Loss: {total_loss/len(dataloader):.4f}")
    torch.save(diffusion.model.state_dict(), "diffusion_unet_pixels.pth")

# -----------------------------
# Main Function
# -----------------------------
def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    # Initialize the UNet model for pixel-space diffusion
    unet = SimpleUNet(in_channels=3, base_channels=64, time_emb_dim=128).to(device)
    diffusion = DiffusionModel(unet, n_steps=1000, device=device)

    # Train the diffusion model on image pixels
    print("Training Diffusion Model in pixel space...")
    train_diffusion(diffusion, train_loader, num_epochs=50, device=device)

    # Generate a sample image using the trained diffusion model
    unet.eval()
    with torch.no_grad():
        sample_shape = (1, 3, img_size, img_size)
        generated = diffusion.sample(sample_shape)
        img = generated.squeeze().cpu().permute(1, 2, 0).numpy()
        plt.figure(figsize=(4, 4))
        plt.imshow(np.clip(img, 0, 1))
        plt.axis('off')
        plt.title("Generated Image")
        plt.savefig("generated_pixel_diffusion.png", bbox_inches='tight')
        plt.show()
        print("Generated image saved as generated_pixel_diffusion.png")

if __name__ == "__main__":
    main()
