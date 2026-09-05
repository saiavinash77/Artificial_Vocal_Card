"""TRD section 5.1 model topology in PyTorch — TRAINING TIME ONLY.

This module is never imported by the runtime pipeline (which uses
onnxruntime). Import it only where torch is installed (training,
export). Topology follows the TRD table exactly:

    Input (B, T, 13)
    -> Linear(13->32) + GELU + LayerNorm                 ~448 params
    -> Conv1d(32->64, k=3) + BN + GELU + Dropout(0.2)     ~6K
    -> Conv1d(64->128, k=3) + BN + GELU + Dropout(0.2)    ~25K
    -> Conv1d(128->256, k=3) + BN + GELU + Dropout(0.2)   ~98K
    -> BiLSTM(256 hidden, bidir)                          ~1.05M
    -> BiLSTM(512->256 hidden, bidir)                     ~1.57M
    -> BiLSTM(512->256 hidden, bidir)                     ~1.57M
    -> MHA(4 heads, d_k=128, d_model=512) + residual + LN ~1.05M
    -> LN + FC(512->256) + GELU + Dropout + FC(256->C)     ~132K
    -> Softmax over C = len(PHONEME_VOCAB)
    Total ~5.5M trainable parameters.
"""

from __future__ import annotations

import torch
import torch.nn as nn

from services.schemas import N_DESCRIPTORS, PHONEME_VOCAB

N_CLASSES = len(PHONEME_VOCAB)  # 40: 39 ARPAbet (incl. HH) + SIL


class ConvBlock(nn.Module):
    def __init__(self, cin: int, cout: int):
        super().__init__()
        self.conv = nn.Conv1d(cin, cout, kernel_size=3, padding=1)
        self.bn = nn.BatchNorm1d(cout)
        self.act = nn.GELU()
        self.drop = nn.Dropout(0.2)

    def forward(self, x):  # x: (B, C, T)
        return self.drop(self.act(self.bn(self.conv(x))))


class PhonemeCNNBiLSTMAttention(nn.Module):
    def __init__(self, n_descriptors: int = N_DESCRIPTORS, n_classes: int = N_CLASSES):
        super().__init__()
        self.input_proj = nn.Sequential(
            nn.Linear(n_descriptors, 32), nn.GELU(), nn.LayerNorm(32))
        self.conv = nn.Sequential(
            ConvBlock(32, 64), ConvBlock(64, 128), ConvBlock(128, 256))
        self.lstm1 = nn.LSTM(256, 256, batch_first=True, bidirectional=True)
        self.lstm2 = nn.LSTM(512, 256, batch_first=True, bidirectional=True)
        self.lstm3 = nn.LSTM(512, 256, batch_first=True, bidirectional=True)
        # embed_dim=512, num_heads=4 => d_k=128 per head (TRD section 5.1)
        self.attn = nn.MultiheadAttention(embed_dim=512, num_heads=4,
                                          batch_first=True)
        self.attn_ln = nn.LayerNorm(512)
        self.head = nn.Sequential(
            nn.LayerNorm(512), nn.Linear(512, 256), nn.GELU(), nn.Dropout(0.2),
            nn.Linear(256, n_classes))

    def forward(self, x):  # x: (B, T, 13)
        h = self.input_proj(x)                  # (B, T, 32)
        h = self.conv(h.transpose(1, 2))         # (B, 256, T)
        h = h.transpose(1, 2)                   # (B, T, 256)
        h, _ = self.lstm1(h)                    # (B, T, 512)
        h, _ = self.lstm2(h)
        h, _ = self.lstm3(h)
        a, _ = self.attn(h, h, h, need_weights=False)
        h = self.attn_ln(h + a)                 # residual + LN
        return self.head(h)                     # (B, T, C) logits


if __name__ == "__main__":  # smoke-check parameter count
    m = PhonemeCNNBiLSTMAttention()
    total = sum(p.numel() for p in m.parameters() if p.requires_grad)
    print(f"trainable parameters: {total:,}")   # TRD target ~5.5M
    x = torch.randn(2, 10, N_DESCRIPTORS)
    print("output:", m(x).shape)                # (2, 10, 40)
