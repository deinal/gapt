import torch
import torch.nn as nn
import pytorch_lightning as pl
import torch.nn.functional as F
from torch.optim.lr_scheduler import LambdaLR
from gapt.utils import cyclic_positional_encoding
from momo import Momo


class GapT(pl.LightningModule):
    def __init__(self, d_input, d_model, n_head, d_feedforward, n_layers, d_output, 
                 learning_rate, dropout_rate, optimizer, use_attention_mask):
        super().__init__()

        self.d_model = d_model
        self.n_head = n_head
        self.learning_rate = learning_rate
        self.optimizer = optimizer
        self.use_attention_mask = use_attention_mask

        self.embedding_cov = nn.Linear(d_input, d_model)
        self.embedding_tgt = nn.Linear(d_output, d_model)
        
        transformer_enc_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_head, dim_feedforward=d_feedforward, 
            activation='gelu', batch_first=True
        )

        self.transformer_enc = nn.TransformerEncoder(
            transformer_enc_layer, num_layers=n_layers
        )

        self.feed_forward = nn.Sequential(
            nn.Linear(2 * d_model, 128),
            nn.GELU(),
            nn.Dropout(dropout_rate),
            nn.Linear(128, 32),
            nn.GELU(),
            nn.Dropout(dropout_rate),
        )
        
        self.head = nn.Linear(32, d_output)
        
    def forward(self, batch):
        positional_encoding_cov = cyclic_positional_encoding(
            batch['minutes_covariates'], self.d_model
        )
        
        positional_encoding_tgt = cyclic_positional_encoding(
            batch['minutes_observations'], self.d_model
        )

        # Embedding inputs
        embedded_cov = self.embedding_cov(batch['covariates']) + positional_encoding_cov 
        embedded_tgt = self.embedding_tgt(batch['interpolated_target']) + positional_encoding_tgt
        
        # Target mask
        mask = batch['mask']
        inverted_mask = ~mask

        # Transformer encoders
        output_cov = self.transformer_enc(embedded_cov, src_key_padding_mask=batch['padded_observations'])

        # src_mask ensures that position i is allowed to attend the unmasked positions.
        # If a BoolTensor is provided, positions with True are not allowed to attend while False values will be unchanged
        if self.use_attention_mask:   
            attention_mask = inverted_mask.permute(0, 2, 1).repeat(1, inverted_mask.size(1), 1) # B, T, T
            attention_mask = attention_mask.repeat(self.n_head, 1, 1) # B*nhead, T, T
            output_tgt = self.transformer_enc(embedded_tgt, mask=attention_mask, src_key_padding_mask=batch['padded_covariates'])
        else:
            output_tgt = self.transformer_enc(embedded_tgt, src_key_padding_mask=batch['padded_covariates'])

        # Concatenate the outputs
        concatenated_output = torch.cat([output_cov, output_tgt], dim=-1)

        # Feed forward
        output = self.feed_forward(concatenated_output)
        output = self.head(output)

        # Masking and output
        output = output * inverted_mask
        output += batch['interpolated_target'] * mask
        return output

    def training_step(self, batch, batch_idx):
        outputs = self.forward(batch)
        loss = F.mse_loss(outputs, batch['target'])
        # loss = quantile_loss(outputs, batch['target'], self.quantiles)
        self.log('train_loss', loss)
        return loss

    def validation_step(self, batch, batch_idx):
        outputs = self.forward(batch)
        loss = F.mse_loss(outputs, batch['target'])
        # loss = quantile_loss(outputs, batch['target'], self.quantiles)
        self.log('val_loss', loss)

    def test_step(self, batch, batch_idx, dataloader_idx):
        outputs = self.forward(batch)
        loss = F.mse_loss(outputs, batch['target'])
        # loss = quantile_loss(outputs, batch['target'], self.quantiles)
        self.log('test_loss', loss)
    
    def lr_lambda(self, current_epoch):
        max_epochs = self.trainer.max_epochs
        start_decay_epoch = int(max_epochs * 0.7) # Start decay at 70% of max epochs

        if current_epoch < start_decay_epoch:
            return 1.0
        else:
            # Compute the decay factor in the range [0, 1]
            decay_factor = (current_epoch - start_decay_epoch) / (max_epochs - start_decay_epoch)
            # Exponential decay down to 1% of the initial learning rate
            return (1.0 - 0.99 * decay_factor)
    
    def configure_optimizers(self):
        if self.optimizer == 'momo':
            optimizer = Momo(self.parameters(), lr=self.learning_rate)
        elif self.optimizer == 'adam':
            optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        else:
            raise ValueError(f'Invalid optimizer: {self.optimizer}')
        
        # scheduler = LambdaLR(optimizer, self.lr_lambda)

        return {
            'optimizer': optimizer,
            # 'lr_scheduler': {
            #     'scheduler': scheduler,
            #     'interval': 'epoch',
            #     'frequency': 1,
            # },
        }