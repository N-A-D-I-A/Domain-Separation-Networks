import os, sys, torch, pdb, datetime
from operator import itemgetter
import torch.autograd as autograd
import torch.nn.functional as F
import torch.utils.data as data
import torch.nn as nn
from tqdm import tqdm
import numpy as np
from sklearn import metrics
import meter
import itertools
import data.data_utils as data_utils


def runDecoder(encoder_outputs, original_inputs, decoder, args):

    if args.cuda:
        original_inputs = original_inputs.cuda()

    true_indices = original_inputs.view(original_inputs.size(0) * original_inputs.size(1), original_inputs.size(2) )

    decoder_hidden = encoder_outputs.view(-1,encoder_outputs.size(2)).unsqueeze(0)

    sos_sym = torch.LongTensor([data_utils.SOS_TOKEN])
    decoder_input = autograd.Variable(sos_sym.expand(encoder_outputs.size(0) * encoder_outputs.size(1), 1))

    loss_criterion = nn.NLLLoss()
    decoder_loss = 0
    #target = autograd.Variable(torch.ones(decoder_input.size(0), 1))

    if args.cuda:
        decoder_input, target = decoder_input.cuda(), target.cuda()

    #last resort: make the decoder loop through only half of the title length
    for di in range(int(original_inputs.size(2)/2)): #original_inputs.data.shape[2] is the seq length
        decoder_out, decoder_hidden = decoder(decoder_input, decoder_hidden)
        topv, topi = torch.topk(decoder_out, 1)
        decoder_input = topi.squeeze(2)

        #decoder_loss += loss_criterion(torch.eq(topi.squeeze(2), true_indices[:,di].unsqueeze(1)).type(torch.FloatTensor), target)
        decoder_loss += loss_criterion(decoder_out.squeeze(1), true_indices[:,di])

    return decoder_loss/int(original_inputs.size(2)/2)


def runEncoderOnQuestions(samples, encoder_model, args):

    bodies, bodies_masks = autograd.Variable(samples['bodies']), autograd.Variable(samples['bodies_masks'])
    if args.cuda:
        bodies, bodies_masks = bodies.cuda(), bodies_masks.cuda()

    out_bodies = encoder_model(bodies, bodies_masks)

    #runDecoder(out_bodies, bodies, decoder, args)

    titles, titles_masks = autograd.Variable(samples['titles']), autograd.Variable(samples['titles_masks'])
    if args.cuda:
        titles, titles_masks = titles.cuda(), titles_masks.cuda()

    out_titles = encoder_model(titles, titles_masks)

    return out_bodies, out_titles


def train_model(train_data, dev_data, source_encoder, target_encoder, shared_encoder,decoder, domain_classifier, args):
    if args.cuda:
        source_encoder, target_encoder, shared_encoder, decoder, domain_classifier = source_encoder.cuda(), target_encoder.cuda(), shared_encoder.cuda(), decoder.cuda(), domain_classifier.cuda()

    encoder_optimizers = []

    for i, model in enumerate([source_encoder, target_encoder, shared_encoder, decoder]):
        parameters = itertools.ifilter(lambda p: p.requires_grad, model.parameters())
        encoder_optimizers.append(torch.optim.Adam(parameters , lr=args.lr[i], weight_decay=args.weight_decay[i]))

    domain_optimizer = torch.optim.Adam(domain_classifier.parameters() , lr=args.lr[4], weight_decay=args.weight_decay[4])

    for epoch in range(1, args.epochs+1):
        print("-------------\nEpoch {}:\n".format(epoch))

        run_epoch(train_data, True, source_encoder, target_encoder, shared_encoder, decoder, domain_classifier, encoder_optimizers, domain_optimizer, args)

        model_path = args.save_path[:args.save_path.rfind(".")] + "_" + str(epoch) + args.save_path[args.save_path.rfind("."):]
        torch.save(encoder_model, model_path)

        print "*******dev********"
        run_epoch(dev_data, False, None, target_encoder, shared_encoder, None, None, encoder_optimizers, None, args)


def test_model(test_data, encoder_model, args):
    if args.cuda:
        encoder_model = encoder_model.cuda()

    print "*******test********"
    run_epoch(test_data, False, (encoder_model, None) , (None, None), args)



def run_epoch(data, is_training, source_encoder, target_encoder, shared_encoder, decoder, domain_classifier, encoder_optimizers, domain_optimizer, args):
    '''
    Train model for one pass of train data, and return loss, acccuracy
    '''

    data_loader = torch.utils.data.DataLoader(
        data,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        drop_last=True)

    losses = []

    if is_training:
        source_encoder.train()
        target_encoder.train()
        shared_encoder.train()
        decoder.train()
        domain_classifier.train()
    else:
        encoder_model.eval()

    nll_loss = nn.NLLLoss()

    auc_met = meter.AUCMeter()

    for batch in tqdm(data_loader):

        cosine_similarity = nn.CosineSimilarity(dim=0, eps=1e-6)
        criterion = nn.MultiMarginLoss(margin=0.4)
        #pdb.set_trace()

        if is_training:
            source_encoder.zero_grad()
            target_encoder.zero_grad()
            shared_encoder.zero_grad()
            decoder.zero_grad()
            domain_classifier.zero_grad()

        ###source question encoder####
        if is_training:
            source_samples = batch['source_samples']
            target_samples = batch['target_samples']

            pri_enc_s_bodies, pri_enc_s_titles = runEncoderOnQuestions(source_samples, source_encoder, args)

            shared_enc_s_bodies, shared_enc_s_titles = runEncoderOnQuestions(source_samples, shared_encoder, args)

            decoder_s_loss = runDecoder(pri_enc_s_titles + shared_enc_s_titles , autograd.Variable(source_samples['titles']), decoder, args)

            shared_enc_t_bodies, shared_enc_t_titles = runEncoderOnQuestions(target_samples, shared_encoder, args)

            pri_enc_t_bodies, pri_enc_t_titles = runEncoderOnQuestions(target_samples, target_encoder, args)

            decoder_t_loss = runDecoder(pri_enc_t_titles + shared_enc_t_titles, autograd.Variable(target_samples['titles']), decoder, args)

            task_hidden_rep = (pri_enc_s_bodies + pri_enc_s_titles + shared_enc_s_bodies + shared_enc_s_titles)/4
        else:
            samples = batch

            shared_enc_bodies, shared_enc_titles = runEncoderOnQuestions(samples, shared_encoder, args)

            pri_enc_t_bodies, pri_enc_t_titles = runEncoderOnQuestions(samples, target_encoder, args)

            task_hidden_rep = (shared_enc_bodies + shared_enc_titles + pri_enc_t_bodies + pri_enc_t_titles)/4


        #Calculate cosine similarities here and construct X_scores
        #expected datastructure of hidden_rep = batchsize x number_of_q x hidden_size
        cs_tensor = autograd.Variable(torch.FloatTensor(task_hidden_rep.size(0), task_hidden_rep.size(1)-1))

        if args.cuda:
            cs_tensor = cs_tensor.cuda()

        #calculate cosine similarity for every query vs. neg q pair
        for j in range(1, task_hidden_rep.size(1)):
            for i in range(task_hidden_rep.size(0)):
                cs_tensor[i, j-1] = cosine_similarity(task_hidden_rep[i, 0, ], task_hidden_rep[i, j, ])

        X_scores = torch.stack(cs_tensor, 0)
        y_targets = autograd.Variable(torch.zeros(task_hidden_rep.size(0)).type(torch.LongTensor))

        if args.cuda:
            y_targets = y_targets.cuda()

        if is_training:
            #####domain classifier#####
            cross_d_questions = batch['question']
            bodies, titles = runEncoderOnQuestions(cross_d_questions, shared_encoder, args)
            avg_hidden_rep = (bodies + titles)/2

            predicted_domains = domain_classifier(avg_hidden_rep)

            true_domains = autograd.Variable(cross_d_questions['domain']).squeeze(1)

            if args.cuda:
                true_domains = true_domains.cuda()

            decoder_loss = (decoder_s_loss + decoder_t_loss)/2
            print "Decoder loss in batch", decoder_loss.data

            domain_classifier_loss = nll_loss(predicted_domains, true_domains)
            print "Domain loss in batch", domain_classifier_loss.data

            #calculate loss
            encoder_loss = criterion(X_scores, y_targets)
            print "Encoder loss in batch", encoder_loss.data

            task_loss = encoder_loss + args.alpha_recon * decoder_loss\
             - args.lambda_d * domain_classifier_loss
            print "Task loss in batch", task_loss.data
            print "\n\n"


            task_loss.backward()

            for encoder_optimizer in encoder_optimizers:
                encoder_optimizer.step()

            domain_optimizer.step()

            losses.append(task_loss.cpu().data[0])

        else:

            for i in range(args.batch_size):

                for j in range(20):
                    y_true = 0
                    if j == 0:
                        y_true = 1

                    x = cs_tensor[i, j].data

                    if args.cuda:
                        x = x.cpu().numpy()
                    else:
                        x = x.numpy()

                    auc_met.add(x, y_true)


    # Calculate epoch level scores
    if is_training:
        avg_loss = np.mean(losses)
        print('Average Train loss: {:.6f}'.format(avg_loss))
        print()
    else:
        print "AUC:", auc_met.value(0.05)
