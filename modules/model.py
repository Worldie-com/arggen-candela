# Author: Xinyu Hua
# Last modified: 2019-2-15
""" hierarchical seq2seq model with attention """

import torch
import torch.nn as nn
import torch.nn.functional as F
from modules import encoder
from modules import decoder
import utils


class Candela(nn.Module):
    """
    A hierarchical sequence-to-sequence model with attention on encoder hidden
    states and a separate memory bank for phrases. The higher level sentence
    decoder serves as a planner to select keyphrases and output sentence type.
    The lower level word decoder generate the actual target outputs.
    """

    def __init__(self, word_emb, word_emb_dim, word_vocab_size):
        super().__init__()
        hidden_size = 512
        self.word_vocab_size = word_vocab_size
        self.global_steps = 0

        self.enc = encoder.EncoderRNN(embedding=word_emb,
                                      emb_size=word_emb_dim,
                                      hidden_size=hidden_size)
        self.sp_dec = decoder.SentencePlanner(hidden_size=hidden_size,
                                              word_emb=word_emb,
                                              word_emb_size=word_emb_dim)

        self.wd_dec = decoder.WordDecoder(hidden_size=hidden_size,
                                          word_emb=word_emb,
                                          word_emb_size=word_emb_dim,
                                          word_vocab_size=word_vocab_size)
        return

    @classmethod
    def load_from_checkpoint(cls, path):
        ckpt = torch.load(path)
        word_emb = nn.Embedding.from_pretrained(ckpt["encoder"]["embedding.weight"])
        model = cls(word_emb=word_emb, word_emb_dim=word_emb.embedding_dim,
                        word_vocab_size=word_emb.num_embeddings)
        model.enc.load_state_dict(ckpt["encoder"])
        model.sp_dec.load_state_dict(ckpt["sp_decoder"])
        model.wd_dec.load_state_dict(ckpt["wd_decoder"])
        return model


    def forward(self, tensor_dict):
        enc_outs, encoder_final = self.enc(tensor_dict["enc_src"],
                                           tensor_dict["enc_src_len"])
        self.sp_dec.init_state(encoder_final=encoder_final)
        self.wd_dec.init_state(encoder_final=encoder_final)

        ph_bank_embedded = self.sp_dec.embedding(tensor_dict["ph_bank_tensor"])
        ph_bank_embedded = torch.sum(ph_bank_embedded, -2)

        sp_dec_outs, ph_attn_probs, ph_attn_logits, st_readouts = self.sp_dec(
            tgt=tensor_dict["ph_sel_tensor"],
            memory_bank=ph_bank_embedded,
            memory_lengths=tensor_dict["ph_bank_len_tensor"])

        wd_dec_state, wd_dec_outs, wd_dec_attn, wd_readouts = \
            self.wd_dec(dec_inputs=tensor_dict["dec_in"],
                        tgt_word_len=tensor_dict["dec_in_len"],
                        enc_memory_bank=enc_outs,
                        enc_memory_len=tensor_dict["enc_src_len"],
                        sent_planner_output=sp_dec_outs,
                        sent_id_template=tensor_dict["dec_sent_id"],
                        sent_mask_template=tensor_dict["dec_mask"])

        return st_readouts, wd_readouts, ph_attn_probs, ph_attn_logits

    # def compute_mtl_losses(self, st_readouts, wd_readouts, st_targets,
    #                        wd_targets, ph_bank_attn,
    #                        ph_bank_sel_ind_targets, mask, st_len, wd_len):
    #
    #     st_loss = self.compute_loss(readouts=st_readouts, targets=st_targets,
    #                                 seq_len=st_len, type_cnt=3)
    #     wd_loss = self.compute_loss(readouts=wd_readouts, targets=wd_targets,
    #                                 seq_len=wd_len,
    #                                 type_cnt=self.word_vocab_size)
    #
    #     attn_loss = self.compute_ph_attn_loss(ph_bank_sel_ind_targets,
    #                                           ph_bank_attn,
    #                                           mask)
    #     return wd_loss, st_loss, attn_loss

    def compute_loss(self, readouts, targets, seq_len, type_cnt):
        readouts_flat = readouts.view(-1, type_cnt)
        log_probs_flat = F.log_softmax(readouts_flat, dim=1)

        dec_targets_flat = targets.view(-1, 1)
        losses_flat = - torch.gather(log_probs_flat, dim=1, index=dec_targets_flat)
        losses = losses_flat.view(*targets.size())

        mask = utils.get_sequence_mask_from_length(seq_len=seq_len, max_len=targets.size(1))
        losses = losses * mask.float()
        loss = losses.sum() / seq_len.float().sum()
        return loss


    def compute_ph_attn_loss(self, ph_sel_target, align_vectors, mask):
        """
        compute cross entropy loss over phrase selection results
        Args:
            ph_sel_target:
            align_vectors:
        """
        loss = F.binary_cross_entropy(input=align_vectors,
                                      target=ph_sel_target.float(),
                                      reduction='none')

        masked_loss = loss * mask.float()
        loss = masked_loss.sum() / mask.sum()
        return loss

    def compute_ppl(self, readouts, targets, seq_len):
        norm_term = seq_len.float().sum()

        readouts_flat = readouts.view(-1, self.word_vocab_size)
        log_probs_flat = F.log_softmax(readouts_flat, dim=1)

        target_flat = targets.view(-1, 1)
        losses_flat = - torch.gather(log_probs_flat, dim=1, index=target_flat)
        losses = losses_flat.view(*targets.size())

        mask = utils.get_sequence_mask_from_length(seq_len=seq_len, max_len=targets.size(1))
        losses = losses * mask.float()

        losses_sum = losses.sum(dim=1)
        probs_normalized = losses_sum / seq_len.float()
        probs_normalized = torch.min(probs_normalized,
                                     100 * torch.ones(probs_normalized.size(), dtype=torch.float).cuda())
        ppl_lst = torch.exp(probs_normalized)
        return torch.mean(ppl_lst)
