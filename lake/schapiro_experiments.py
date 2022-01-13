import os
import csv
import json
import argparse
import logging
import utils
import torch
import torch.nn
import numpy as np
import datetime
import glob
from scipy import stats
from torch.utils.tensorboard import SummaryWriter
from cls_module.cls import CLS
from datasets.sequence_generator import SequenceGenerator, SequenceGeneratorGraph, SequenceGeneratorTriads
from datasets.omniglot_one_shot_dataset import OmniglotTransformation
from datasets.omniglot_per_alphabet_dataset import OmniglotAlphabet
# from Visualisations import HeatmapPlotter
from torchvision import transforms
from oneshot_metrics import OneshotMetrics

hebbian = False


LOG_EVERY = 20
LOG_EVERY_EVAL = 1
VAL_EVERY = 20
SAVE_EVERY = 1
MAX_VAL_STEPS = 100
MAX_PRETRAIN_STEPS = -1
VAL_SPLIT = 0.175

def main():
    parser = argparse.ArgumentParser(description='Pair structure. Replicate of Schapiro')

    if hebbian:
        parser.add_argument('-c', '--config', nargs="?", type=str, default='./definitions/aha_config_Schapiro_hebb.json',
                        help='Configuration file for experiments.')
    else:
        parser.add_argument('-c', '--config', nargs="?", type=str, default='./definitions/aha_config_Schapiro_episodic.json',
                        help='Configuration file for experiments.')

    parser.add_argument('-l', '--logging', nargs="?", type=str, default='warning',
                        help='Logging level.')

    args = parser.parse_args()

    logging_level = getattr(logging, args.logging.upper(), None)
    logging.basicConfig(level=logging_level)

    with open(args.config) as config_file:
        config = json.load(config_file)

    seed_ltm = config['seeds']
    seeds = config['seeds']
    experiment = config.get('experiment_name')
    learning_type = config.get('learning_type')
    components = config.get('test_components')
    now = datetime.datetime.now()
    experiment_time = now.strftime("%Y%m%d-%H%M%S")

    if experiment not in ["pairs_structure", "community_structure", "associative_inference"]:
        experiment = "pairs_structure"
        print("Experiment NOT specified. Must be pairs_structure, community_structure or associative_inference "
              "Set to pairs_structure by default.")

    main_summary_dir = utils.get_summary_dir(experiment + "/" + learning_type, experiment_time,
                                             main_folder=True)

    with open(main_summary_dir + '/info_exp.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    image_tfms = transforms.Compose([
        transforms.ToTensor(),
        OmniglotTransformation(resize_factor=config['image_resize_factor'])])

    image_shape = config['image_shape']
    final_shape = config['pairs_shape']
    alphabet_name = config.get('alphabet')
    pretrained_model_path = config.get('pretrained_model_path', None)
    pearson_r_test = torch.zeros((1, 2))
    if experiment == 'community_structure':
        pearson_r_test = torch.zeros((1, 4))
    pearson_r_test = {k: pearson_r_test for k in components}
    characters = config.get('characters')
    pearson_r_tensor = torch.zeros((characters, characters))
    pearson_r_tensor = pearson_r_tensor[None, :, :]
    pearson_r_tensor = {k: pearson_r_tensor for k in components}

    for _ in range(seeds):
        seed = np.random.randint(1, 10000)
        utils.set_seed(seed)

        # Pretraining
        # ---------------------------------------------------------------------------
        summary_dir = utils.get_summary_dir(experiment + "/" + learning_type, experiment_time, main_folder=False, seed=str(seed))
        writer = SummaryWriter(log_dir=summary_dir)
        if pretrained_model_path:
            # summary_dir = pretrained_model_path
             #writer = SummaryWriter(log_dir=summary_dir)
            model = CLS(image_shape, config, device=device, writer=writer, output_shape=final_shape).to(device)

            model_path = os.path.join(pretrained_model_path, 'pretrained_model_*')
            print(model_path)
            # Find the latest file in the directory
            latest = max(glob.glob(model_path), key=os.path.getctime)

            trained_model_path = latest

            # Load only the LTM part
            pretrained_dict = torch.load(trained_model_path)
            pretrained_dict = {k: v for k, v in pretrained_dict.items() if k.startswith('ltm')}
            model.load_state_dict(pretrained_dict)

        else:
            start_epoch = 1
            utils.set_seed(seed_ltm)
            model = CLS(image_shape, config, device=device, writer=writer, output_shape=final_shape).to(device)

            dataset = OmniglotAlphabet('./data', alphabet_name, False, writer_idx='any', download=True,
                                       transform=image_tfms,
                                       target_transform=None)

            val_size = round(VAL_SPLIT * len(dataset))
            train_size = len(dataset) - val_size

            train_set, val_set = torch.utils.data.random_split(dataset, [train_size, val_size])

            train_loader = torch.utils.data.DataLoader(train_set, batch_size=config['pretrain_batch_size'], shuffle=True)
            val_loader = torch.utils.data.DataLoader(val_set, batch_size=config['pretrain_batch_size'], shuffle=True)

            # Pre-train the model
            for epoch in range(start_epoch, config['pretrain_epochs'] + 1):

                for batch_idx, (data, target) in enumerate(train_loader):

                    if 0 < MAX_PRETRAIN_STEPS < batch_idx:
                        print("Pretrain steps, {}, has exceeded max of {}.".format(batch_idx, MAX_PRETRAIN_STEPS))
                        break
                    labels = list(set(target))
                    target = torch.tensor([labels.index(value) for value in target])

                    data = data.to(device)
                    target = target.to(device)

                    losses, _ = model(data, labels=target if model.is_ltm_supervised() else None, mode='pretrain')
                    pretrain_loss = losses['ltm']['memory']['loss'].item()

                    if batch_idx % LOG_EVERY == 0:
                        print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                            epoch, batch_idx, len(train_loader),
                            100. * batch_idx / len(train_loader), pretrain_loss))

                    if batch_idx % VAL_EVERY == 0 or batch_idx == len(train_loader) - 1:
                        logging.info("\t--- Start validation")

                        with torch.no_grad():
                            for batch_idx_val, (val_data, val_target) in enumerate(val_loader):

                                if batch_idx_val >= MAX_VAL_STEPS:
                                    print("\tval batch steps, {}, has exceeded max of {}.".format(batch_idx_val,
                                                                                                  MAX_VAL_STEPS))
                                    break


                                val_data = val_data.to(device)
                                target = target.to(device)

                                val_losses, _ = model(val_data, labels=val_target if model.is_ltm_supervised() else None,
                                                      mode='validate')
                                val_pretrain_loss = val_losses['ltm']['memory']['loss'].item()

                                if batch_idx_val % LOG_EVERY == 0:
                                    print('\tValidation for Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                                        epoch, batch_idx_val, len(val_loader),
                                        100. * batch_idx_val / len(val_loader), val_pretrain_loss))

                if epoch % SAVE_EVERY == 0:
                    trained_model_path = os.path.join(summary_dir, 'pretrained_model_' + str(epoch) + '.pt')
                    print('Saving model to:', trained_model_path)
                    torch.save(model.state_dict(), trained_model_path)
                pretrained_model_path = summary_dir

        # Study and Recall
        # ---------------------------------------------------------------------------
        print('-------- Few-shot Evaluation (Study and Recall) ---------')

        length = config.get('sequence_length')
        communities = config.get('communities')
        batch_size = config['study_batch_size']
        idx_study = config.get('character_idx_study')
        idx_recall = config.get('character_idx_recall')
        single_recall = config.get('test_single_characters')


        # Create sequence
        if experiment == "pairs_structure":
            sequence_study = SequenceGenerator(characters, length, learning_type)
            sequence_recall = SequenceGenerator(characters, length, learning_type)
        elif experiment == "community_structure":
            sequence_study = SequenceGeneratorGraph(characters, length, learning_type, communities)
            sequence_recall = SequenceGeneratorGraph(characters, length, learning_type, communities)
        elif experiment == "associative_inference":
            sequence_study = SequenceGeneratorTriads(characters, length, learning_type, batch_size)
            sequence_recall = SequenceGeneratorTriads(characters, length, learning_type, batch_size)

        predictions = []
        pairs_inputs = []


        for stm_epoch in range(config['train_epochs']):

            sequence_study_tensor = torch.FloatTensor(sequence_study.sequence)
            sequence_recall_tensor = torch.FloatTensor(sequence_recall.sequence)
            study_loader = torch.utils.data.DataLoader(sequence_study_tensor, batch_size=batch_size, shuffle=False)
            recall_loader = torch.utils.data.DataLoader(sequence_recall_tensor, batch_size=batch_size, shuffle=False)

            pair_sequence_dataset = enumerate(zip(study_loader, recall_loader))

            # Load images from the selected alphabet from a specific writer or random writers
            alphabet = OmniglotAlphabet('./data', alphabet_name, True, config['variation_training'], idx_study, download=True,
                                        transform=image_tfms, target_transform=None)

            alphabet_recall = OmniglotAlphabet('./data', alphabet_name, True, config.get('variation'), idx_recall, download=True,
                                               transform=image_tfms, target_transform=None)

            if config['variation_training']:
                second_alphabet = alphabet_recall
            else:
                second_alphabet = alphabet

            labels_study = sequence_study.core_label_sequence
            main_pairs, _ = convert_sequence_to_images(alphabet=alphabet, sequence=labels_study, element='both',
                                                       main_labels=labels_study, second_alphabet=second_alphabet)
            main_pairs_flat = torch.flatten(main_pairs, start_dim=1)
            single_characters = [alphabet_recall[a][0] for a in range(0, characters)]
            single_characters = torch.stack(single_characters)

            # Initialise metrics
            oneshot_metrics = OneshotMetrics()

            for idx, (study_set, recall_set) in pair_sequence_dataset:

                study_paired_data, study_paired_target = convert_sequence_to_images(alphabet=alphabet, second_alphabet=second_alphabet,
                                                                       sequence=study_set, element='both',
                                                                        main_labels=labels_study)


                study_data_A, study_target = convert_sequence_to_images(alphabet=second_alphabet,
                                                                              sequence=study_set, element='first',
                                                                              main_labels=labels_study)
                study_target = torch.tensor(study_target, dtype=torch.long, device=device)
                study_data_B, _ = convert_sequence_to_images(alphabet=alphabet,
                                                                              sequence=study_set, element='second',
                                                                              main_labels=labels_study)
                _, study_data_A = model(study_data_A, study_target, mode='validate')
                _, study_data_B = model(study_data_B, study_target, mode='validate')
                study_data = config['activation_coefficient']*study_data_A['ltm']['memory']['output'].detach() + study_data_B['ltm']['memory']['output'].detach()

                study_data = study_data.to(device)
                study_target = study_target.to(device)

                if single_recall:
                    recall_data = single_characters.repeat(-(-batch_size//characters), 1, 1, 1)
                    recall_data = recall_data[0:batch_size]
                    recall_target = list(range(single_characters.size()[0]))
                    recall_target = recall_target * -(-batch_size//characters)
                    recall_data_blank = torch.zeros_like(recall_data)
                    recall_paired_data = torch.cat([recall_data, recall_data_blank], dim=3)

                    del recall_target[batch_size:len(recall_target)]
                    recall_target = torch.tensor(recall_target, dtype=torch.long, device=device)
                    _, recall_data = model(recall_data, recall_target, mode='validate')
                    recall_data = recall_data['ltm']['memory']['output'].detach()
                else:

                    recall_data_A, recall_target = convert_sequence_to_images(alphabet=alphabet_recall,
                                                                                sequence=recall_set, element='first',
                                                                                main_labels=labels_study)
                    recall_target = torch.tensor(recall_target, dtype=torch.long, device=device)
                    recall_data_B, _ = convert_sequence_to_images(alphabet=alphabet_recall,
                                                                     sequence=recall_set, element='second',
                                                                     main_labels=labels_study)
                    recall_paired_data = torch.cat([recall_data, recall_data_B], dim=3)
                    _, recall_data_A = model(recall_data_A, recall_target, mode='validate')
                    _, recall_data_B = model(recall_data_B, recall_target, mode='validate')
                    recall_data = recall_data_A['ltm']['memory']['output'].detach() + recall_data_B['ltm']['memory'][
                            'output'].detach()

                recall_data = recall_data.to(device)
                recall_target = torch.tensor(recall_target, dtype=torch.long, device=device)
                recall_target = recall_target.to(device)

                # Reset to saved model
                model.reset()

                # Study
                # --------------------------------------------------------------------------
                for step in range(config['late_response_steps']):

                    study_train_losses, _ = model(study_data, study_target, mode='study', ec_inputs=study_data,
                                                      paired_inputs=study_paired_data)
                    study_train_loss = study_train_losses['stm']['memory']['loss']

                    if hebbian:
                        print('Losses batch {}, ite {}: \t EC_CA3:{:.6f}\
                         \t ca3_ca1: {:.6f}'.format(idx, step,study_train_loss['ec_ca3'].item(),
                                                    study_train_loss['ca3_ca1'].item()))
                    else:
                        print('Losses batch {}, ite {}: \t PR:{:.6f}\
                        PR mismatch: {:.6f} \t ca3_ca1: {:.6f}'.format(idx, step,
                                                                       study_train_loss['pr'].item(),
                                                                       study_train_loss['pr_mismatch'].item(),
                                                                       study_train_loss['ca3_ca1'].item()))

                    if step == (config['early_response_step']-1) or step == (config['late_response_steps']-1):
                        with torch.no_grad():
                            _, recall_outputs = model(recall_data, recall_target, mode='recall', ec_inputs=recall_data,
                                                      paired_inputs=recall_paired_data)
                            cos = torch.nn.CosineSimilarity(dim=0, eps=1e-6)
                            for component in components:
                                #if component == 'ltm':
                                #    recall_outputs_flat = torch.flatten(recall_outputs["ltm"]["memory"]["decoding"],
                                #                                        start_dim=1)
                                if component == 'dg':
                                    recall_outputs_flat = torch.flatten(recall_outputs["stm"]["memory"][component],
                                                                    start_dim=1)
                                if component == 'pr':
                                    recall_outputs_flat = torch.flatten(recall_outputs["stm"]["memory"][component]['pr_out'],
                                                                        start_dim=1)
                                if component == 'ec_ca3':
                                    recall_outputs_flat = torch.flatten(recall_outputs["stm"]["memory"][component]['ca3_cue'],
                                            start_dim=1)
                                if component == 'ca3_ca1':
                                    recall_outputs_flat = torch.flatten(
                                        recall_outputs["stm"]["memory"][component]['decoding'],
                                        start_dim=1)
                                if component == 'ca1':
                                    recall_outputs_flat = torch.flatten(
                                        recall_outputs["stm"]["memory"][component]['decoding'],
                                        start_dim=1)
                                if component == 'recon_pair':
                                    recall_outputs_flat = torch.flatten(
                                        recall_outputs["stm"]["memory"]['decoding'],
                                        start_dim=1)
                                    similarity = [[cos(a, b) for b in main_pairs_flat.cpu()] for a in recall_outputs_flat.cpu()]
                                    similarity_idx = [t.index(max(t)) for t in similarity]
                                    predictions.extend([[labels_study[a] for a in similarity_idx]])

                                recall_outputs_flat = recall_outputs_flat[0:characters].cpu()
                                pearson_r = [[stats.pearsonr(a, b)[0] for a in recall_outputs_flat] for b in
                                             recall_outputs_flat]
                                pearson_r = torch.tensor(pearson_r)
                                pearson_r_tensor[component] = torch.cat((pearson_r_tensor[component], pearson_r[None, :, :]), 0)

                                if experiment == 'pairs_structure':
                                    pearson_all_pattern = sum([pearson_r[i, j] for (i, j) in sequence_study.test_sequence])/len(sequence_study.test_sequence)
                                    pearson_core_pattern = sum([pearson_r[i, j] for (i, j) in sequence_study.core_sequence]) / len(sequence_study.core_sequence)
                                    tmp_pearson_test = torch.tensor([[pearson_core_pattern, pearson_all_pattern]])
                                if experiment == 'associative_inference':
                                    pearson_transitive_pattern = sum([pearson_r[i, j] for (i, j) in sequence_study.test_sequence]) / len(sequence_study.test_sequence)
                                    pearson_direct_pattern = sum([pearson_r[i, j] for (i, j) in sequence_study.core_sequence]) / len(sequence_study.core_sequence)
                                    pearson_base_pattern = sum([pearson_r[i, j] for (i, j) in sequence_study.base_sequence]) / len(sequence_study.base_sequence)
                                    tmp_pearson_test = torch.tensor([[pearson_transitive_pattern - pearson_base_pattern, pearson_direct_pattern-pearson_base_pattern]])
                                if experiment == 'community_structure':
                                    pearson_within_internal = sum([pearson_r[i, j] for (i, j) in sequence_study.graph_sequences[0]]) / len(sequence_study.graph_sequences[0])
                                    pearson_within_boundary = sum([pearson_r[i, j] for (i, j) in sequence_study.graph_sequences[1]]) / len(sequence_study.graph_sequences[1])
                                    pearson_across_boundary = sum([pearson_r[i, j] for (i, j) in sequence_study.graph_sequences[2]]) / len(sequence_study.graph_sequences[2])
                                    pearson_across_other = sum([pearson_r[i, j] for (i, j) in sequence_study.graph_sequences[3]]) / len(sequence_study.graph_sequences[3])
                                    tmp_pearson_test = torch.tensor([[pearson_within_internal, pearson_within_boundary, pearson_across_boundary, pearson_across_other]])

                                pearson_r_test[component] = torch.cat((pearson_r_test[component], tmp_pearson_test), 0)



                pairs_inputs.extend([[(int(a[0]), int(a[1])) for a in study_set]])

                # Recall
                # --------------------------------------------------------------------------
                with torch.no_grad():

                    if 'metrics' in config and config['metrics']:
                        metrics_config = config['metrics']

                        metrics_len = [len(value) for key, value in metrics_config.items()]
                        assert all(x == metrics_len[0] for x in metrics_len), 'Mismatch in metrics config'

                        for i in range(metrics_len[0]):
                            primary_feature = utils.find_json_value(metrics_config['primary_feature_names'][i], model.features)
                            primary_label = utils.find_json_value(metrics_config['primary_label_names'][i], model.features)
                            secondary_feature = utils.find_json_value(metrics_config['secondary_feature_names'][i],
                                                                      model.features)
                            secondary_label = utils.find_json_value(metrics_config['secondary_label_names'][i], model.features)
                            comparison_type = metrics_config['comparison_types'][i]
                            prefix = metrics_config['prefixes'][i]

                            oneshot_metrics.compare(prefix,
                                                    primary_feature, primary_label,
                                                    secondary_feature, secondary_label,
                                                    comparison_type=comparison_type)

                    if hebbian:
                        stm_feature = 'stm_ec_ca3'
                    else:
                        stm_feature = 'stm_pr'

                    oneshot_metrics.compare(prefix='pr_rf_',
                                             primary_features=model.features['recall'][stm_feature],
                                             primary_labels=model.features['recall']['labels'],
                                             secondary_features=model.features['study'][stm_feature],
                                             secondary_labels=model.features['study']['labels'],
                                             comparison_type='match_mse')

                    oneshot_metrics.report()

                    summary_names = [
                        'study_paired_inputs',
                        'study_' + stm_feature,
                        'study_stm_ca3',

                        'recall_paired_inputs',
                        'recall_' + stm_feature,
                        'recall_stm_ca3',
                        'recall_stm_recon'
                    ]

                    summary_images = []
                    for name in summary_names:
                        mode_key, feature_key = name.split('_', 1)

                        summary_features = model.features[mode_key][feature_key]
                        if len(summary_features.shape) > 2:
                            summary_features = summary_features.permute(0, 2, 3, 1)

                        summary_shape, _ = utils.square_image_shape_from_1d(np.prod(summary_features.data.shape[1:]))
                        summary_shape[0] = summary_features.data.shape[0]

                        summary_image = (name, summary_features, summary_shape)
                        summary_images.append(summary_image)

                    utils.add_completion_summary(summary_images, summary_dir, idx, save_figs=True)

# Save results
        predictions_early = predictions[0:predictions.__len__():2]
        predictions_late = predictions[1:predictions.__len__():2]

        with open(main_summary_dir + '/predictions_early' + str(stm_epoch) + '_' + str(seed) + '.csv', 'w', encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(predictions_early)

        with open(main_summary_dir + '/predictions_late' + str(stm_epoch) + '_' + str(seed)+'.csv', 'w', encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(predictions_late)

        with open(main_summary_dir + '/pair_inputs' + str(stm_epoch) + '_' + str(
                    seed) + '.csv', 'w', encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(pairs_inputs)

        oneshot_metrics.report_averages()
        writer.flush()
        writer.close()

    pearson_r_early = {a: pearson_r_tensor[a][1:pearson_r_tensor[a].shape[0]:2] for a in pearson_r_tensor}
    pearson_r_late = {a: pearson_r_tensor[a][2:pearson_r_tensor[a].shape[0] + 1:2] for a in pearson_r_tensor}
    pearson_r_early = {a: torch.mean(pearson_r_early[a], 0) for a in pearson_r_early}
    pearson_r_late = {a: torch.mean(pearson_r_late[a], 0) for a in pearson_r_late}

    pearson_r_early_test = {a: pearson_r_test[a][1:pearson_r_test[a].shape[0]:2] for a in pearson_r_test}
    pearson_r_late_test = {a: pearson_r_test[a][2:pearson_r_test[a].shape[0] + 1:2] for a in
                                 pearson_r_test}


    for a in pearson_r_early_test.keys():
        with open(main_summary_dir + '/pearson_early_test_' + a + '.csv',
                  'w', encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(np.stack(pearson_r_early_test[a].numpy(), 0))

    for a in pearson_r_late_test.keys():
        with open(main_summary_dir + '/pearson_late_test_' + a + '.csv',
                  'w', encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(np.stack(pearson_r_late_test[a].numpy(), 0))

    for a in pearson_r_early.keys():
        with open(main_summary_dir + '/pearson_early_' + a + '.csv', 'w',
                  encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(pearson_r_early[a].numpy())

    for a in pearson_r_late.keys():
        with open(main_summary_dir + '/pearson_late_' + a + '.csv', 'w',
                  encoding='UTF8') as f:
            writer_file = csv.writer(f)
            writer_file.writerows(pearson_r_late[a].numpy())

    for a in pearson_r_early.keys():
         heatmap_early = HeatmapPlotter(main_summary_dir, "pearson_early_" + a)
         heatmap_late = HeatmapPlotter(main_summary_dir, "pearson_late_" + a)
         heatmap_early.create_heatmap()
         heatmap_late.create_heatmap()

    bars = BarPlotter(main_summary_dir, pearson_r_early.keys())
    bars.create_bar()

def convert_sequence_to_images(alphabet, sequence, main_labels, element='first', second_alphabet=None):
    if element == 'both':
        pairs_images = [torch.cat((second_alphabet[int(a[0])][0], alphabet[int(a[1])][0]), 2) for a in sequence]
    if element == 'first':
        pairs_images = [alphabet[int(a[0])][0] for a in sequence]
    if element == 'second':
        pairs_images = [alphabet[int(a[1])][0] for a in sequence]
    labels = [(alphabet[int(a[0])][1], alphabet[int(a[1])][1]) for a in sequence]
    labels = [main_labels.index(value) for value in labels]

    pairs_images = torch.stack(pairs_images, 0)

    return pairs_images, labels


if __name__ == '__main__':
    main()
