# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from omegaconf.omegaconf import OmegaConf, open_dict
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelSummary
from pytorch_lightning.callbacks.timer import Timer
from pytorch_lightning.plugins.environments.torchelastic_environment import TorchElasticEnvironment
from pytorch_lightning.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from pytorch_lightning.trainer.connectors.checkpoint_connector import CheckpointConnector

from nemo.collections.nlp.data.machine_translation.preproc_mt_data import MTDataPreproc
from nemo.collections.nlp.models.language_modeling.megatron_bart_model import MegatronBARTModel
from nemo.collections.nlp.models.language_modeling.megatron_t5_model import MegatronT5Model
from nemo.collections.nlp.models.machine_translation.megatron_nmt_model import MegatronNMTModel
from nemo.collections.nlp.parts.nlp_overrides import (
    GradScaler,
    MegatronHalfPrecisionPlugin,
    NLPDDPPlugin,
    NLPSaveRestoreConnector,
    PipelineMixedPrecisionPlugin,
)
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import StatelessTimer, exp_manager


@hydra_runner(config_path="conf", config_name="aayn_base_megatron")
def main(cfg) -> None:
    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f'\n{OmegaConf.to_yaml(cfg)}')

    megatron_amp_o2 = cfg.model.get('megatron_amp_O2', False)
    plugins = [
        NLPDDPPlugin(
            no_ddp_communication_hook=True,
            gradient_as_bucket_view=cfg.model.gradient_as_bucket_view,
            find_unused_parameters=False,
        )
    ]
    if cfg.trainer.precision in [16, 'bf16']:
        scaler = None
        if cfg.trainer.precision == 16:
            scaler = GradScaler(
                init_scale=cfg.model.get('native_amp_init_scale', 2 ** 32),
                growth_interval=cfg.model.get('native_amp_growth_interval', 1000),
                hysteresis=cfg.model.get('hysteresis', 2),
            )
        if megatron_amp_o2:
            plugins.append(MegatronHalfPrecisionPlugin(precision=cfg.trainer.precision, device='cuda', scaler=scaler))
        else:
            plugins.append(PipelineMixedPrecisionPlugin(precision=cfg.trainer.precision, device='cuda', scaler=scaler))

    if cfg.get('cluster_type', None) == 'BCP':
        plugins.append(TorchElasticEnvironment())

    trainer = Trainer(plugins=plugins, **cfg.trainer, callbacks=[ModelSummary(max_depth=3)])

    # tokenizers will be trained and and tarred training data will be created if needed
    # model config is then updated
    if cfg.model.preproc_out_dir is not None:
        MTDataPreproc(cfg=cfg.model, trainer=trainer)

    exp_manager(trainer, cfg.exp_manager)

    # update resume from checkpoint found by exp_manager
    if cfg.model.resume_from_checkpoint is not None:
        resume_from_checkpoint = cfg.model.resume_from_checkpoint
    else:
        resume_from_checkpoint = trainer._checkpoint_connector.resume_from_checkpoint_fit_path
    logging.info(f'Resuming training from checkpoint: {resume_from_checkpoint}')

    trainer._checkpoint_connector = CheckpointConnector(trainer, resume_from_checkpoint=resume_from_checkpoint)
    # Override timer callback to a stateless one
    for idx, callback in enumerate(trainer.callbacks):
        if isinstance(callback, Timer):
            trainer.callbacks[idx] = StatelessTimer(cfg.trainer.max_time,)

    # hydra interpolation does not work here as the interpolation key is lost when PTL saves hparams
    with open_dict(cfg):
        cfg.model.precision = cfg.trainer.precision

    if hasattr(cfg.model, 'pretrained_model_path') and cfg.model.pretrained_model_path is not None:
        if not hasattr(cfg.model, 'pretrained_model_type'):
            raise ValueError(f"Pretrained model type must be in [T5, BART].")

        assert cfg.model.pretrained_model_type in ['T5', 'BART']
        if cfg.model.pretrained_model_type == 'T5':
            pretrained_cfg = MegatronT5Model.restore_from(
                cfg.model.pretrained_model_path, trainer=trainer, return_config=True
            )
        else:
            pretrained_cfg = MegatronBARTModel.restore_from(
                cfg.model.pretrained_model_path, trainer=trainer, return_config=True
            )
        OmegaConf.set_struct(pretrained_cfg, True)
        with open_dict(pretrained_cfg):
            pretrained_cfg.masked_softmax_fusion = False
            # Set source and target language/multilingual
            pretrained_cfg.src_language = cfg.model.src_language
            pretrained_cfg.tgt_language = cfg.model.tgt_language
            pretrained_cfg.multilingual = cfg.model.multilingual
            pretrained_cfg.shared_tokenizer = True

            # Max generation delta
            pretrained_cfg.max_generation_delta = cfg.model.max_generation_delta

            # Set label smoothing
            pretrained_cfg.label_smoothing = cfg.model.label_smoothing

            # Set tokenizer paths:
            pretrained_cfg.encoder_tokenizer = pretrained_cfg.tokenizer
            pretrained_cfg.decoder_tokenizer = pretrained_cfg.tokenizer

            # Pre-trained models should use the legacy sentencepiece tokenizer ex: mT5
            pretrained_cfg.encoder_tokenizer.sentencepiece_legacy = True
            pretrained_cfg.decoder_tokenizer.sentencepiece_legacy = True

            # Override dropout
            pretrained_cfg.hidden_dropout = cfg.model.hidden_dropout
            pretrained_cfg.attention_dropout = cfg.model.attention_dropout

            # Override precision
            pretrained_cfg.precision = cfg.model.precision  # Set above from trainer.precision

            # Override data and global/micro batch size.
            pretrained_cfg.train_ds = cfg.model.train_ds
            pretrained_cfg.validation_ds = cfg.model.validation_ds
            pretrained_cfg.test_ds = cfg.model.test_ds

            pretrained_cfg.micro_batch_size = cfg.model.micro_batch_size
            pretrained_cfg.global_batch_size = cfg.model.global_batch_size

            # Class target for the new class being restored.
            pretrained_cfg.target = (
                "nemo.collections.nlp.models.machine_translation.megatron_nmt_model.MegatronNMTModel"
            )

            # Optimizer overrides.
            pretrained_cfg.optim = cfg.model.optim

        model = MegatronNMTModel.restore_from(
            cfg.model.pretrained_model_path,
            trainer=trainer,
            override_config_path=pretrained_cfg,
            save_restore_connector=NLPSaveRestoreConnector(),
        )
    else:
        model = MegatronNMTModel(cfg.model, trainer)
    if cfg.do_training:
        trainer.fit(model)

    if cfg.do_testing:
        trainer.test(model)


if __name__ == '__main__':
    main()
