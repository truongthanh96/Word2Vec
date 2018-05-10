import urllib.request
import collections
import math
import os
import random
import zipfile
import datetime as dt
import pandas as pd

import numpy as np
import tensorflow as tf

from sklearn.decomposition import PCA

import matplotlib.pyplot as plt
import re

import utilities
from datamodel import DataModel, IterBatchDataModel


def maybe_download(filename, url, expected_bytes):
    """Download a file if not present, and make sure it's the right size."""
    if not os.path.exists(filename):
        filename, _ = urllib.request.urlretrieve(url + filename, filename)
    statinfo = os.stat(filename)
    if statinfo.st_size == expected_bytes:
        print('Found and verified', filename)
    else:
        print(statinfo.st_size)
        raise Exception(
            'Failed to verify ' + filename + '. Can you get to it with a browser?')
    return filename


# Read the data into a list of strings.
def read_data(filename):
    """Extract the first file enclosed in a zip file as a list of words."""
    with zipfile.ZipFile(filename) as f:
        data = tf.compat.as_str(f.read(f.namelist()[0])).split()
    return data


def build_dataset(words, n_words):
    """Process raw inputs into a dataset."""
    count = [['UNK', -1]]
    count.extend(collections.Counter(words).most_common(n_words - 1))
    dictionary = dict()
    for word, _ in count:
        dictionary[word] = len(dictionary)
    data = list()
    unk_count = 0
    for word in words:
        if word in dictionary:
            index = dictionary[word]
        else:
            index = 0  # dictionary['UNK']
            unk_count += 1
        data.append(index)
    count[0][1] = unk_count
    reversed_dictionary = dict(zip(dictionary.values(), dictionary.keys()))
    return data, count, dictionary, reversed_dictionary


class Tf_Word2Vec:
    def __init__(self, save_path, main_path, save_every_iteration=1000, vocabulary_size=10000):
        self.data_index = 0
        self.save_path = save_path
        self.main_path = main_path
        self.progress_save_path = "{}/progress.json".format(main_path)
        self.save_every_iteration = save_every_iteration
        self.progress = {
            "iteration": 0,
            "current_num": 0
        }

        batch_size = 128
        embedding_size = 300  # Dimension of the embedding vector.
        skip_window = 2  # How many words to consider left and right.
        num_skips = 2  # How many times to reuse an input to generate a label.

        # We pick a random validation set to sample nearest neighbors. Here we limit the
        # validation samples to the words that have a low numeric ID, which by
        # construction are also the most frequent.
        valid_size = 16  # Random set of words to evaluate similarity on.
        valid_window = 100  # Only pick dev samples in the head of the distribution.
        valid_examples = np.random.choice(valid_window, valid_size, replace=False)
        num_sampled = 64  # Number of negative examples to sample.
        self.session = None
        self.final_embeddings = None
        self.train_data = None
        graph = tf.Graph()

        with graph.as_default():
            # Input data.
            train_inputs = tf.placeholder(tf.int32, shape=[batch_size])
            train_context = tf.placeholder(tf.int32, shape=[batch_size, 1])
            valid_dataset = tf.constant(valid_examples, dtype=tf.int32)

            # Look up embeddings for inputs.
            embeddings = tf.Variable(
                tf.random_uniform([vocabulary_size, embedding_size], -1.0, 1.0))
            embed = tf.nn.embedding_lookup(embeddings, train_inputs)

            # Construct the variables for the NCE loss
            nce_weights = tf.Variable(
                tf.truncated_normal([vocabulary_size, embedding_size],
                                    stddev=1.0 / math.sqrt(embedding_size)))
            nce_biases = tf.Variable(tf.zeros([vocabulary_size]))

            nce_loss = tf.reduce_mean(
                tf.nn.nce_loss(weights=nce_weights,
                               biases=nce_biases,
                               labels=train_context,
                               inputs=embed,
                               num_sampled=num_sampled,
                               num_classes=vocabulary_size))

            optimizer = tf.train.GradientDescentOptimizer(1.0).minimize(nce_loss)

            # Compute the cosine similarity between minibatch examples and all embeddings.
            norm = tf.sqrt(tf.reduce_sum(tf.square(embeddings), 1, keep_dims=True))
            normalized_embeddings = embeddings / norm
            valid_embeddings = tf.nn.embedding_lookup(
                normalized_embeddings, valid_dataset)
            similarity = tf.matmul(
                valid_embeddings, normalized_embeddings, transpose_b=True)

            # Add variable initializer.
            init = tf.global_variables_initializer()

            self.nn_var = (
                train_inputs, train_context, valid_dataset, embeddings, nce_loss, optimizer, normalized_embeddings,
                similarity, init)
            # self.saver = tf.train.Saver(max_to_keep=4, keep_checkpoint_every_n_hours=2)
            self.saver = tf.train.Saver()

        self.train_data = IterBatchDataModel(batch_size=batch_size, max_vocab_size=vocabulary_size,
                                             num_skip=num_skips, skip_window=skip_window)

        self.var = (
            vocabulary_size, batch_size, embedding_size, skip_window,
            num_skips, valid_size, valid_window, valid_examples, num_sampled, graph)

    def restore_last_training_if_exists(self):
        progress_path = self.progress_save_path
        if os.path.exists(progress_path):
            print("Progress found, loading progress {}".format(progress_path))
            self.load_progress(progress_path)

    def save_progress(self, save_path):
        train_data_progress = self.train_data.get_progress()
        saved_data = {
            "train_data_progress": train_data_progress,
            "tensor_progress": self.progress
        }
        utilities.save_json_object(saved_data, save_path)

    def load_progress(self, save_path):
        saved_data = utilities.load_json_object(save_path)
        self.train_data.load_progress(saved_data["train_data_progress"])
        self.progress = saved_data["tensor_progress"]

    def load_model_if_exists(self, iteration=None):
        model_path = self.save_path
        if iteration is None:
            path = "{}.meta".format(model_path)
        else:
            path = "{}-{}.meta".format(model_path, iteration)
        if os.path.exists(path):
            print("Data found! Loading saved model {}".format(model_path))
            self.load_model(model_path)

    def init_data(self, csv_path, preload=False, is_folder_path=False):
        self.train_data.init_data_model(csv_path, preload_data=preload,
                                        print_percentage=False, is_folder_path=is_folder_path)

    def save_data(self, save_data_model_path):
        utilities.save_simple_object(self.train_data.data_model, save_data_model_path)

    def load_data(self, save_data_model_path):
        self.train_data.data_model = utilities.load_simple_object(save_data_model_path)

    def init_vocab(self):
        self.train_data.build_vocabulary()

    def load_vocab(self, vocab_path):
        self.train_data.set_vocabulary(utilities.load_simple_object(vocab_path))

    def save_vocab(self, vocab_path):
        utilities.save_simple_object(self.train_data.get_vocabulary(), vocab_path)

    def train(self, num_steps=2):
        (vocabulary_size, batch_size, embedding_size, skip_window,
         num_skips, valid_size, valid_window, valid_examples, num_sampled, graph) = self.var

        (train_inputs, train_context, valid_dataset, embeddings, nce_loss, optimizer, normalized_embeddings, similarity,
         init) = self.nn_var

        dictionary = self.train_data.dictionary
        reversed_dictionary = self.train_data.reversed_dictionary

        nce_start_time = dt.datetime.now()

        session = tf.Session(graph=graph)
        self.session = session
        # We must initialize all variables before we use them.
        init.run(session=session)
        print('Initialized')

        average_loss = 0
        for step in range(self.progress["current_num"], num_steps):
            self.progress["current_num"] = step
            for (batch_inputs, batch_context) in self.train_data:
                feed_dict = {train_inputs: batch_inputs, train_context: batch_context}

                # We perform one update step by evaluating the optimizer op (including it
                # in the list of returned values for session.run()
                _, loss_val = session.run([optimizer, nce_loss], feed_dict=feed_dict)
                average_loss += loss_val
                self.progress["iteration"] += 1

                if self.save_every_iteration and self.progress["iteration"] % self.save_every_iteration == 0:
                    utilities.print_current_datetime()
                    print("Saving iteration no {}".format(self.progress["iteration"]))
                    self.save_model(self.save_path, self.progress["iteration"])
                    self.save_progress(self.progress_save_path)
                    # self.train_data.save_progress(self.progress_save_path)

                    sim = similarity.eval(session=session)
                    for i in range(valid_size):
                        valid_word = reversed_dictionary[valid_examples[i]]
                        top_k = 8  # number of nearest neighbors
                        nearest = (-sim[i, :]).argsort()[1:top_k + 1]
                        log_str = 'Nearest to %s:' % valid_word
                        for k in range(top_k):
                            close_word = reversed_dictionary[nearest[k]]
                            log_str = '%s %s,' % (log_str, close_word)
                        print(log_str)

        self.final_embeddings = normalized_embeddings.eval(session=session)
        nce_end_time = dt.datetime.now()
        print(
            "NCE method took {} seconds to run 100 iterations".format((nce_end_time - nce_start_time).total_seconds()))

    def save_model(self, path, global_step=None):
        save_path = self.saver.save(self.session, path, global_step=global_step)
        print("Model saved in path: %s" % save_path)

    def load_model(self, path):
        (vocabulary_size, batch_size, embedding_size, skip_window,
         num_skips, valid_size, valid_window, valid_examples, num_sampled, graph) = self.var
        (train_inputs, train_context, valid_dataset, embeddings, nce_loss, optimizer, normalized_embeddings, similarity,
         init) = self.nn_var

        self.session = tf.Session(graph=graph)
        self.saver.restore(self.session, path)
        self.final_embeddings = normalized_embeddings.eval(session=self.session)

    def similar_by(self, word, top_k=8):
        dictionary = self.train_data.dictionary
        reversed_dictionary = self.train_data.reversed_dictionary

        norm = np.sqrt(np.sum(np.square(self.final_embeddings), 1))
        norm = np.reshape(norm, (len(dictionary), 1))
        normalized_embeddings = self.final_embeddings / norm
        valid_embeddings = normalized_embeddings[dictionary[word]]
        similarity = np.matmul(
            valid_embeddings, np.transpose(normalized_embeddings), )

        nearest = (-similarity[:]).argsort()[1:top_k + 1]
        log_str = 'Nearest to %s:' % word
        for k in range(top_k):
            close_word = reversed_dictionary[nearest[k]]
            log_str = '%s %s,' % (log_str, close_word)
        return log_str

    def draw(self):
        embeddings = self.final_embeddings
        reversed_dictionary = self.train_data.reversed_dictionary
        words_np = []
        words_label = []
        for i in range(0, len(embeddings)):
            words_np.append(embeddings[i])
            words_label.append(reversed_dictionary[i])

        pca = PCA(n_components=2)
        pca.fit(words_np)
        reduced = pca.transform(words_np)

        plt.rcParams["figure.figsize"] = (20, 20)
        for index, vec in enumerate(reduced):
            if index < 1000:
                x, y = vec[0], vec[1]
                plt.scatter(x, y)
                plt.annotate(words_label[index], xy=(x, y))
        plt.show()

