#!/usr/bin/env python

import argparse
import cPickle as pickle
import json
import numpy as np
import os
import random
import sys
import time
import torch
import torch.nn as nn

from operator import itemgetter
from torch.autograd import Variable
from torch.optim import SGD, Adamax
from Input import Dictionary, Data, pad, read_data, create2d, one_hot
from OurModel import OurModel

def init_parser():
  parser = argparse.ArgumentParser()
  parser.add_argument('--run_type', default='train',
                      help = "One of either test or train.")
  parser.add_argument('--train_json',
                      help = "Path to the input train json file containing SQuAD train data.")
  parser.add_argument('--dev_json',
                      help = "Path to the input dev json file containing SQuAD dev data.")
  parser.add_argument('--train_pickle',
                      help = "Path to read/dump train pickle file to.")
  parser.add_argument('--dev_pickle',
                      help = "Path to read/dump dev pickle file to.")
  parser.add_argument('--predictions_output_json',
                      help = "When using run_type test, output predictions will be written to this \
                              json")
  parser.add_argument('--dump_pickles', action='store_true',
                      help = "Whether the train/dev pickles must be dumped. Input jsons must be \
                              provided to create these pickles.")
  parser.add_argument('--max_train_articles', type=int, default=-1,
                      help = "Maximum number of training articles to use, while reading from the \
                              train json file.")
  parser.add_argument('--max_dev_articles', type=int, default=-1,
                      help = "Maximum number of dev articles to use, while reading from the dev \
                              json file.")
  parser.add_argument('--embed_size', type=int, default=300,
                      help = "Embedding size to use for inputs. This *MUST* match the GloVe dimensions \
                              when disable_glove is not set.")
  parser.add_argument('--hidden_size', type=int, default=300,
                      help = "Hidden dimension sizes to use throughout the network. For bi-directional \
                              layers, each direction is half this size.")
  parser.add_argument('--attention_size', type=int, default=150,
                      help = "Number of dimensions of the intermediate spaces to which vectors are cast \
                              before they are used to compute attention values.")
  parser.add_argument('--learning_rate', type=float, default=0.01,
                      help = "Learning rate used by the optimizer.")
  parser.add_argument('--decay_rate', type=float, default=0.90,
                      help = "The learning rate is multiplied by this value after every epoch.")
  parser.add_argument('--glove_path', default='../../data/glove/glove.840B.300d.txt',
                      help = "Path to the GloVe vectors to use for the embedding layer.")
  parser.add_argument('--disable_glove', action='store_true',
                      help = "When provided, GloVe vectors are not used, and an embedding layer is \
                              learned in an end-to-end manner.")
  parser.add_argument('--ckpt', type=int, default=0,
                      help = "Checkpoint number to resume from. If 0, starts training from scratch.")
  parser.add_argument('--model_file',
                      help = "A particular model file to load the model from. Especially useful for \
                              test runs.")
  parser.add_argument('--epochs', type=int, default=50,
                      help = "Number of epochs to train the model for.")
  parser.add_argument('--model_dir', default='./',
                      help = "Directory to dump the trained models after every epoch, as well as the \
                              dev predictions json files after every epoch.")
  parser.add_argument('--batch_size', type=int, default=64,
                      help = "Batch size to use during training.")
  parser.add_argument('--test_batch_size', type=int, default=32,
                      help = "Batch size to use during development and test data passes.")
  parser.add_argument('--optimizer', default='Adamax',
                      help = "Optimizer to use. One of either 'SGD', 'Adamax' or 'Adadelta'.")
  parser.add_argument('--debug', action='store_true',
                      help = "If true, GloVe vectors are not read, and train and dev data sizes are \
                              reduced to 3200 each. Useful for debugging passes of every part of the \
                              code.")
  parser.add_argument('--dropout', type=float, default=0.4,
                      help = "Dropout drop probability between layers and modules of the network.")
  parser.add_argument('--cuda', action='store_true',
                      help = "Whether the model must be trained of an NVIDIA GPU device.")
  parser.add_argument('--max_answer_span', type=int, default=15,
                      help = "Maximum length of answers during prediction. Search is performed over spans \
                              of this length.")
  parser.add_argument('--num_preprocessing_layers', type=int, default=2,
                      help = "Number of passage and question pre-processing layers.")
  parser.add_argument('--num_postprocessing_layers', type=int, default=2,
                      help = "Number of post-processing layers, before the answer pointer network.")
  parser.add_argument('--num_matchlstm_layers', type=int, default=1,
                      help = "Number of MatchLSTM layers to use.")
  parser.add_argument('--f1_loss_multiplier', type=float, default=2.0,
                      help = "Multiply the Expected F1 loss by this value. Useful as this loss has a \
                              smaller magnitude than the MLE loss.")
  parser.add_argument('--f1_loss_threshold', type=float, default=-1.0,
                      help = "Only penalize F1 values below this threshold. -1 is the distribution loss.")
  parser.add_argument('--combine_losses', action='store_true',
                      help = "Combine both losses, instead of training in a multi-task setting.")
  parser.add_argument('--model_description',
                      help = "A useful model description to keep track of which model was run.")
  return parser


#------------- ---------------- Preprocess data -------------------------------#
def read_and_process_data(args):
  assert not (args.train_json == None and args.train_pickle == None)
  assert not (args.dev_json == None and args.dev_pickle == None)

  #----------------------- Read train, dev and test data ------------------------#
  train_data, dev_data = \
    read_data(args.train_json, args.train_pickle, args.dev_json, args.dev_pickle,
              args.max_train_articles, args.max_dev_articles, args.dump_pickles)
  #------------------------------------------------------------------------------#

  # Our dev is also test...
  test_data = dev_data

  train = train_data.data
  dev = dev_data.data
  test = dev_data.data
  batch_size = args.batch_size
  test_batch_size = args.test_batch_size
  train_ques_to_para = train_data.question_to_paragraph
  dev_ques_to_para = dev_data.question_to_paragraph
  test_ques_to_para = dev_data.question_to_paragraph
  train_tokenized_paras = train_data.tokenized_paras
  dev_tokenized_paras = dev_data.tokenized_paras
  test_tokenized_paras = dev_data.tokenized_paras

  # Sort data by increasing passage+question, for efficient batching.
  # Data format = (tokenized_question, tokenized_answer, question_id).
  print "Sorting datasets in decreasing order of (para + question) lengths."
  train.sort(cmp=lambda x,y:\
      len(x[0]) + len(train_tokenized_paras[train_ques_to_para[x[2]]]) -\
      (len(y[0]) + len(train_tokenized_paras[train_ques_to_para[y[2]]])),
      reverse=True)
  dev.sort(cmp=lambda x,y:\
      len(x[0]) + len(dev_tokenized_paras[dev_ques_to_para[x[2]]]) -\
      (len(y[0]) + len(dev_tokenized_paras[dev_ques_to_para[y[2]]])),
      reverse=True)
  test.sort(cmp=lambda x,y:\
      len(x[0]) + len(test_tokenized_paras[test_ques_to_para[x[2]]]) -\
      (len(y[0]) + len(test_tokenized_paras[test_ques_to_para[y[2]]])),
      reverse=True)
  print "Done."

  # Debug flag reduces size of input data, for testing purposes.
  if args.debug:
    train = train[:320]
    dev = dev[:320]
    test = test[:320]

  network_ids = ["0"] if args.f1_loss_multiplier == 0 else ["0", "1"]
  if args.combine_losses:
    train_order = [ (i, ["0", "1"]) for i in range(0, len(train), batch_size) ]
  else:
    train_order = [ (i,[x]) for i in range(0, len(train), batch_size) \
                      for x in network_ids ]
  dev_order = [ i for i in range(0, len(dev), test_batch_size) ]
  test_order = [ i for i in range(0, len(test), test_batch_size) ]
  print "Done."

  return train, dev, test, batch_size, test_batch_size, train_ques_to_para,\
         dev_ques_to_para, test_ques_to_para, train_tokenized_paras,\
         dev_tokenized_paras, test_tokenized_paras, train_order, dev_order,\
         test_order, train_data, dev_data, test_data
#------------------------------------------------------------------------------#


#------------------------------ Create model ----------------------------------#
def build_model(args, vocab_size, index_to_word, word_to_index, num_pos_tags):
  config = { 'embed_size' : args.embed_size,
             'vocab_size' : vocab_size,
             'hidden_size' : args.hidden_size,
             'attention_size' : args.attention_size,
             'decay_rate' : args.decay_rate,
             'lr' : args.learning_rate,
             'dropout' : args.dropout,
             'glove_path' : args.glove_path,
             'use_glove' : not args.disable_glove,
             'ckpt': args.ckpt,
             'optimizer': args.optimizer,
             'index_to_word': index_to_word,
             'word_to_index': word_to_index,
             'cuda': args.cuda,
             'num_pos_tags': num_pos_tags,
             'f1_loss_multiplier': args.f1_loss_multiplier,
             'f1_loss_threshold' : args.f1_loss_threshold,
             'num_preprocessing_layers': args.num_preprocessing_layers,
             'num_postprocessing_layers': args.num_postprocessing_layers,
             'num_matchlstm_layers': args.num_matchlstm_layers }
  print "Building model."
  model = OurModel(config, args.debug)
  print "Done!"
  sys.stdout.flush()

  print "%d OOV words." % model.oov_count
  print "OOV Words:",
  print model.oov_list[:10]

  if not args.disable_glove:
    print "Embedding dim:", model.embedding.shape

  if args.cuda:
    model = model.cuda()

  return model, config
#------------------------------------------------------------------------------#

#--------------------------- Create an input minibatch ------------------------#
def get_batch(batch, ques_to_para, tokenized_paras, paras_tags, num_tags):
  # Variable length question, answer and paragraph sequences for batch.
  ques_lens_in = [ len(example[0]) for example in batch ]
  paras_in = [ tokenized_paras[ques_to_para[example[2]]] \
                 for example in batch ]
  paras_tags_in = [ paras_tags[ques_to_para[example[2]]] \
                      for example in batch ]
  paras_lens_in = [ len(para) for para in paras_in ]
  max_ques_len = max(ques_lens_in)
  max_para_len = max(paras_lens_in)

  # ans_in.shape = (2, batch)
  ans_in = np.array([ example[1] for example in batch ]).T
  sent_in = np.array([ example[5] for example in batch ]).T

  # f1_mat_in.shape = (batch, seq_len, seq_len)
  f1_mat_in = np.array([ create2d(example[3], 0, max_para_len, example[1][0]) \
                           for example in batch])
  # Fixed-length (padded) input sequences with shape=(seq_len, batch).
  ques_in = np.array([ pad(example[0], 0, max_ques_len)\
                         for example in batch ]).T
  paras_in = np.array([ pad(para, 0, max_para_len) for para in paras_in ]).T

  # Fixed-length (padded) pos-tag inputs.
  question_tags = np.array([ pad([ one_hot(tag, num_tags) for tag in example[4] ],
                                 one_hot(-1, num_tags), max_ques_len) \
                               for example in batch ])
  question_tags = np.transpose(question_tags, (1, 0, 2))
  passage_tags = np.array([ pad([ one_hot(tag, num_tags) for tag in para_tags ],
                                 one_hot(-1, num_tags), max_para_len) \
                               for para_tags in paras_tags_in ])
  passage_tags = np.transpose(passage_tags, (1, 0, 2))

  passage_input = (paras_in, paras_lens_in)
  question_input = (ques_in, ques_lens_in)
  answer_input = ans_in
  answer_sentence_input = sent_in

  return passage_input, question_input, answer_input, f1_mat_in, question_tags,\
         passage_tags, answer_sentence_input
#------------------------------------------------------------------------------#


#--------------- Get the answers from predicted distributions------------------#
def get_batch_answers(args, batch, all_predictions, distributions, data):
  # Get numpy arrays out of the CUDA tensors.
  for i in range(len(distributions)):
    for j in range(len(distributions[i])):
      for k in range(len(distributions[i][j])):
        distributions[i][j][k] = distributions[i][j][k].data.cpu().numpy()

  # Add all batch qids to predictions dict, if they don't already exist.
  qids = [ example[2] for example in batch ]
  for qid in qids:
    if not qid in all_predictions:
      all_predictions[qid] = []

  tokenized_paras = data.tokenized_paras
  ques_to_para = data.question_to_paragraph
  paras_in = [ tokenized_paras[ques_to_para[example[2]]] \
                 for example in batch ]
  paras_lens_in = [ len(para) for para in paras_in ]

  mle = 0
  f1 = 1
  best_idxs = []
  # distributions => (mle/f1,forward/backward,start/end,batch,values). Phew!
  for idx in range(len(batch)):
    best_prob = -1
    best = [0, 0]
    max_end = paras_lens_in[idx]
    for j, start_prob in enumerate(distributions[mle][0][0][idx][:max_end]):
      cur_end_idx = max_end if args.max_answer_span == -1 \
                            else j + args.max_answer_span
      end_idx = np.argmax(distributions[mle][0][1][idx][j:cur_end_idx] * \
                          distributions[mle][1][0][idx][j:cur_end_idx] * \
                          distributions[f1][0][1][idx][j:cur_end_idx] * \
                          distributions[f1][1][0][idx][j:cur_end_idx])
      prob = distributions[mle][0][1][idx][j+end_idx] * start_prob \
             * distributions[mle][1][1][idx][j] * distributions[mle][1][0][idx][j+end_idx] \
             * distributions[f1][0][1][idx][j+end_idx] * distributions[f1][0][0][idx][j] \
             * distributions[f1][1][1][idx][j] * distributions[f1][1][0][idx][j+end_idx]
      if prob > best_prob:
        best_prob = prob
        best = [j, j+end_idx]
    best_idxs.append(best)

  answers = [ tokenized_paras[ques_to_para[qids[idx]]][start:end+1] \
                for idx, (start, end) in enumerate(best_idxs) ]
  answers = [ " ".join([ data.dictionary.get_word(idx) for idx in ans ]) \
                for ans in answers ]

  for qid, answer in zip(qids, answers):
    all_predictions[qid] = answer

  return qids, answers
#------------------------------------------------------------------------------#


def train_model(args):
  # Read and process data
  train, dev, test, batch_size, test_batch_size, train_ques_to_para,\
  dev_ques_to_para, test_ques_to_para, train_tokenized_paras,\
  dev_tokenized_paras, test_tokenized_paras, train_order, dev_order, test_order,\
  train_data, dev_data, test_data = read_and_process_data(args)

  # Build model
  num_pos_tags = len(train_data.dictionary.pos_tags)
  model, config = build_model(args, train_data.dictionary.size(),
                              train_data.dictionary.index_to_word,
                              train_data.dictionary.word_to_index,
                              num_pos_tags)

  if not os.path.exists(args.model_dir):
    os.mkdir(args.model_dir)

  #------------------------------ Train System ----------------------------------#
  # Should we resume running from an existing checkpoint?
  last_done_epoch = config['ckpt']
  if last_done_epoch > 0:
    model = model.load(args.model_dir, last_done_epoch)
    print "Loaded model."
    if not args.disable_glove:
      print "Embedding shape:", model.embedding.shape

  if args.model_file is not None:
    model = model.load_from_file(args.model_file)
    print "Loaded model from %s." % args.model_file

  start_time = time.time()
  print "Starting training."

  if args.optimizer == "SGD":
    print "Using SGD optimizer."
    optimizer = SGD(model.parameters(), lr = args.learning_rate)
  elif args.optimizer == "Adamax":
    print "Using Adamax optimizer."
    optimizer = Adamax(model.parameters(), lr = args.learning_rate)
    if last_done_epoch > 0:
      if os.path.exists(args.model_dir + "/optim_%d.pt" % last_done_epoch):
        optimizer = torch.load(args.model_dir + "/optim_%d.pt" % last_done_epoch)
      else:
        print "Optimizer saved state not found. Not loading optimizer."
  else:
    assert False, "Unrecognized optimizer."
  print(model)

  print "Starting training."
  for EPOCH in range(last_done_epoch+1, args.epochs):
    start_t = time.time()
    train_loss_sum = 0.0
    model.train()
    for i, (num, network_ids) in enumerate(train_order):
      print "\rTrain epoch %d, %.2f s - (Done %d of %d)" %\
            (EPOCH, (time.time()-start_t)*(len(train_order)-i-1)/(i+1), i+1,
             len(train_order)),

      # Create next batch by getting lengths and padding
      train_batch = train[num:num+batch_size]

      # Zero previous gradient.
      model.zero_grad()

      # Predict on the network_id assigned to this minibatch.
      model(*get_batch(train_batch, train_ques_to_para, train_tokenized_paras,
                       train_data.paras_tags, num_pos_tags),
            networks = network_ids)
      model.loss.backward()
      optimizer.step()
      train_loss_sum += model.loss.data[0]

      print "Loss Total: %.5f, Cur: %.5f (in time %.2fs)" % \
            (train_loss_sum/(i+1), model.loss.data[0], time.time() - start_t),
      sys.stdout.flush()
      del model.loss

    print "\nLoss: %.5f (in time %.2fs)" % \
          (train_loss_sum/len(train_order), time.time() - start_t)

    # End of epoch.
    random.shuffle(train_order)
    model.zero_grad()
    model.save(args.model_dir, EPOCH)

    # Updating LR for optimizer
    for param in optimizer.param_groups:
      param['lr'] *= config['decay_rate']
    if args.optimizer == "Adamax":
      torch.save(optimizer, args.model_dir + "/optim_%d.pt" % EPOCH)

    # Run pass over dev data.
    dev_start_t = time.time()
    dev_loss_sum = 0.0
    all_predictions = {}
    print "\nRunning on Dev."

    model.eval()
    for i, num in enumerate(dev_order):
      print "\rDev: %.2f s (Done %d of %d)" %\
            ((time.time()-dev_start_t)*(len(dev_order)-i-1)/(i+1), i+1,
            len(dev_order)),

      dev_batch = dev[num:num+test_batch_size]

      # distributions[{0,1}][{0,1}].shape = (batch, max_passage_len)
      # Predict using both networks.
      distributions = \
        model(*get_batch(dev_batch, dev_ques_to_para, dev_tokenized_paras,
                         dev_data.paras_tags, num_pos_tags),
              networks = ["0", "1"])

      # Add predictions to all answers.
      get_batch_answers(args, dev_batch, all_predictions, distributions,
                        dev_data)

      dev_loss_sum += model.loss.data[0]
      print "[Average loss : %.5f, Cur: %.5f]" % (dev_loss_sum/(i+1), model.loss.data[0]),
      sys.stdout.flush()
      del model.loss

    # Print dev stats for epoch
    print "\nDev Loss: %.4f (in time: %.2f s)" %\
          (dev_loss_sum/len(dev_order), (time.time() - dev_start_t))

    # Dump the results json in the required format
    print "Dumping prediction results."
    json.dump(
      all_predictions,
      open(args.model_dir + "/dev_predictions_" + str(EPOCH) + ".json", "w"))
    print "Done."
#------------------------------------------------------------------------------#


#-------------------------------- Test model ----------------------------------#
def test_model(args):
  # Read and process data
  train, dev, test, batch_size, test_batch_size, train_ques_to_para,\
  dev_ques_to_para, test_ques_to_para, train_tokenized_paras,\
  dev_tokenized_paras, test_tokenized_paras, train_order, dev_order, test_order,\
  train_data, dev_data, test_data = read_and_process_data(args)

  # Build model
  num_pos_tags = len(train_data.dictionary.pos_tags)
  model, config = build_model(args, train_data.dictionary.size(),
                              train_data.dictionary.index_to_word,
                              train_data.dictionary.word_to_index,
                              num_pos_tags)
  print(model)

  #------------------------- Reload and test model ----------------------------#
  if args.model_file is not None:
    model = model.load_from_file(args.model_file)
    print "Loaded model from %s." % args.model_file
  else:
    last_done_epoch = config['ckpt']
    model = model.load(args.model_dir, last_done_epoch)
    print "Loaded model."
    if not args.disable_glove:
      print "Embedding shape:", model.embedding.shape

  test_start_t = time.time()
  test_loss_sum = 0.0
  all_predictions = {}
  attention_starts = {}
  attention_ends = {}
  model.eval()

  for i, num in enumerate(test_order):
    print "\rTest: %.2f s (Done %d of %d) " %\
          ((time.time()-test_start_t)*(len(test_order)-i-1)/(i+1), i+1,
          len(test_order)),

    test_batch = test[num:num+test_batch_size]
    batch_size = len(test_batch)

    # distributions[{0,1}][{0,1}].shape = (batch, max_passage_len)
    distributions = \
        model(*get_batch(test_batch, test_ques_to_para, test_tokenized_paras,
                         test_data.paras_tags, num_pos_tags),
              networks = ["0", "1"])

    # Add predictions to all answers.
    get_batch_answers(args, test_batch, all_predictions, distributions, test_data)

    # Dump start and end attention distributions from "0" id network.
    ans_in = np.array([ example[1] for example in test_batch ]).T
    qids = [ example[2] for example in test_batch ]
    for idx in range(batch_size):
      if qids[idx] in attention_starts:
        attention_starts[qids[idx]][1].append(ans_in[0][idx])
      else:
        attention_starts[qids[idx]] = (distributions[0][0][idx], [ans_in[0][idx]])
      if qids[idx] in attention_ends:
        attention_ends[qids[idx]][1].append(ans_in[0][idx])
      else:
        attention_ends[qids[idx]] = (distributions[0][1][idx], [ans_in[1][idx]])

    test_loss_sum += model.loss.data[0]
    print "[Average loss : %.5f]" % (test_loss_sum/(i+1)),
    sys.stdout.flush()
    del model.loss

  # Print stats
  print "\nTest Loss: %.4f (in time: %.2f s)" %\
        (test_loss_sum/len(test_order), (time.time() - test_start_t))

  # Dump the results json in the required format
  print "Dumping prediction results."
  json.dump(all_predictions, open(args.predictions_output_json, "w"))

  # Dump attention start and end distributions.
  pickle.dump(attention_starts,
              open(args.predictions_output_json + "_starts.p", "wb"))
  pickle.dump(attention_ends,
              open(args.predictions_output_json + "_ends.p", "wb"))
  print "Done."
#------------------------------------------------------------------------------#

if __name__ == "__main__":
  args = init_parser().parse_args()
  assert args.model_description is not None, "Model description must be provided."
  print "-" * 10 + "Arguments:" + "-" * 10
  for arg in sorted(vars(args)):
    print "--" + arg, getattr(args, arg),
  print "\n" + "-" * 30
  if args.run_type == "train":
    train_model(args)
  elif args.run_type == "test":
    test_model(args)
  else:
    print "Invalid run type:", args.run_type

