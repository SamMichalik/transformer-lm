import os
import time
import math
from functools import partial

import numpy as np
import tensorflow as tf
from tensorflow.contrib.training import HParams

from opt import adam, warmup_constant, warmup_cosine, warmup_linear

def default_hparams():
    # commented out are original values
    return HParams(
        n_vocab=0,
        n_ctx=50,#1024,
        n_embd=256,#756,
        n_head=4,#12,
        n_layer=3,#12,
        max_grad_norm=5.,
        warmup_steps=2000,
        positional_encoding='learned', # or 'fixed'
        lr=6.25e-5,
        lr_warmup=0.002,
        l2=0.01,
        vector_l2='store_true',
        b1=0.9,
        b2=0.999,
        e=1e-8,
        batch_size=50,
        n_epochs=10,
        sample_every=10000,
        log_dir='../transformer-lm-logs/'
    )

class Network():
    def __init__(self, seed=42):
        graph = tf.Graph()
        graph.seed = seed
        self.session = tf.Session(graph=graph)
        self.signature = time.strftime("%Y-%m-%d-%H-%M-%S")

    def construct(self, hparams):
        self.hparams = hparams
        with self.session.graph.as_default():

            # Construct the model
            self.X = tf.placeholder(tf.int32, [None, None], name="inputs")
            self.Y = tf.placeholder(tf.int32, [None, None], name="ground_truth")

            results = {}
            batch, sequence = shape_list(self.X)

            if hparams.positional_encoding == 'learned':
                wpe = tf.get_variable('wpe', [hparams.n_ctx, hparams.n_embd],
                                     initializer=tf.random_normal_initializer(stddev=0.01))
            elif hparams.positional_encoding == 'fixed':
                wpe = positional_encoding(hparams.n_ctx, hparams.n_embd)
            wte = tf.get_variable('wte', [hparams.n_vocab, hparams.n_embd],
                                initializer=tf.random_normal_initializer(stddev=0.02))
            h = tf.gather(wte, self.X) + tf.gather(wpe, position_for(self.X, 0))

            for layer in range(hparams.n_layer):
                h = block(h, 'h%d' % layer, hparams=hparams)
            h = norm(h, 'ln_f')

            h_flat = tf.reshape(h, [batch*sequence, hparams.n_embd])
            logits = tf.matmul(h_flat, wte, transpose_b=True)
            logits = tf.reshape(logits, [batch, sequence, hparams.n_vocab])
            results['logits'] = logits

            # Compute the loss
            logits = results['logits']
            loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=self.Y, logits=logits)
            self.loss = tf.reduce_mean(loss, axis=None)  

            # Attach the optimizer
            global_step = tf.train.create_global_step()
            self.train_ops, gradient_norm, self.lr = get_train_ops(hparams, self.loss, global_step)

            # Summaries
            log_dir = 'logs' if hparams.log_dir is None else hparams.log_dir
            if not os.path.exists(log_dir):
                os.mkdir(log_dir)    

            summary_writer = tf.contrib.summary.create_file_writer(os.path.join(log_dir, self.signature), 
                                                                    flush_millis=10 * 1000)
            self.summaries = {}
            with summary_writer.as_default(), tf.contrib.summary.record_summaries_every_n_global_steps(10):
                self.summaries["train"] = [tf.contrib.summary.scalar("train_loss", self.loss), 
                                            tf.contrib.summary.scalar("train/gradient_norm", gradient_norm),
                                            tf.contrib.summary.scalar("train/learning_rate", self.lr)]

            self.session.run(tf.global_variables_initializer())
            with summary_writer.as_default():
                tf.contrib.summary.initialize(session=self.session, graph=self.session.graph)

            with open(os.path.join(log_dir, self.signature, 'hparams.json'), 'w') as hpf:
                hpf.write(hparams.to_json())


    def train_epoch(self, dataset):
        dataset.reset_batch_pointer()
        for _ in range(dataset.num_batches):
            x, y = dataset.next_batch()
            self.session.run([ self.summaries, self.loss, self.train_ops ], 
                            { self.X: x, self.Y: y })

    # def create_summaries(self):
    #     if not os.path.exists(args.log_dir):
    #         os.mkdir(args.log_dir)

    #     summary_writer = tf.contrib.summary.create_file_writer('logs/%s' % self.signature, flush_millis=10 * 1000)
    #     self.summaries = {}
    #     with summary_writer.as_default(), tf.contrib.summary.record_summaries_every_n_global_steps(10):
    #         self.summaries["train"] = [tf.contrib.summary.scalar("train/loss", self.loss), 
    #                                     tf.contrib.summary.scalar("train/gradient_norm", gradient_norm)]

    #     self.session.run(tf.global_variables_initializer())
    #     with summary_writer.as_default():
    #         tf.contrib.summary.initialize(session=self.session, graph=self.session.graph)

    #     with open('logs/%s/hparams.json' % self.signature, 'w') as hpf:
    #         hpf.write(hparams.to_json())

def positional_encoding(n_ctx, n_embd):
    assert n_embd % 2 == 0
    p = tf.expand_dims(tf.range(0, n_ctx, dtype=tf.float32), 1)
    i = tf.range(0, n_embd, 2, dtype=tf.float32)
    d = tf.exp(tf.multiply(i, -(math.log(10000.0) / n_embd)))
    d = tf.expand_dims(d, 0)
    sin = tf.math.sin(d * p)
    cos = tf.math.cos(d * p)
    pe = tf.reshape(tf.stack([sin,cos], axis=2), [tf.shape(sin)[0], -1])
    assert pe.shape[0] == n_ctx and pe.shape[1] == n_embd
    return pe

def split_states(x, n):
    *start, m = shape_list(x)
    return tf.reshape(x, start + [n, m//n])

def merge_states(x):
    *start, a, b = shape_list(x)
    return tf.reshape(x, start + [a*b])

def softmax(x, axis=-1):
    x = x - tf.reduce_max(x, axis=axis, keepdims=True)
    ex = tf.exp(x)
    return ex / tf.reduce_sum(ex, axis=axis, keepdims=True)

def gelu(x):
    return 0.5*x*(1+tf.tanh(np.sqrt(2/np.pi)*(x+0.044715*tf.pow(x, 3))))

def norm(x, scope, *, axis=-1, epsilon=1e-5):
    with tf.variable_scope(scope):
        n_state = x.shape[-1].value
        g = tf.get_variable('g', [n_state], initializer=tf.constant_initializer(1))
        b = tf.get_variable('b', [n_state], initializer=tf.constant_initializer(0))
        u = tf.reduce_mean(x, axis=axis, keepdims=True)
        s = tf.reduce_mean(tf.square(x-u), axis=axis, keepdims=True)
        x = (x - u) * tf.rsqrt(s + epsilon)
        x = x*g + b
        return x

def conv1d(x, scope, nf, *, w_init_stdev=0.02):
    with tf.variable_scope(scope):
        *start, nx = shape_list(x)
        w = tf.get_variable('w', [1, nx, nf], initializer=tf.random_normal_initializer(stddev=w_init_stdev))
        b = tf.get_variable('b', [nf], initializer=tf.constant_initializer(0))
        c = tf.reshape(tf.matmul(tf.reshape(x, [-1, nx]), tf.reshape(w, [-1, nf])+b), start+[nf])
        return c

def attention_mask(nd, ns, *, dtype):
    i = tf.range(nd)[:, None]
    j = tf.range(ns)
    m = i >= j - ns + nd
    return tf.cast(m, dtype)

def attn(x, scope, n_state, *, hparams):
    assert x.shape.ndims == 3 # [batch, sequence, features]
    assert n_state % hparams.n_head == 0
    
    def split_heads(x):
        # [batch, sequence, features] => [batch, heads, sequence, features]
        return tf.transpose(split_states(x, hparams.n_head), [0, 2, 1, 3])

    def merge_heads(x):
        return merge_states(tf.transpose(x, [0, 2, 1, 3]))

    def mask_attn_weights(w):
        _, _, nd, ns = shape_list(w)
        b = attention_mask(nd, ns, dtype=w.dtype)
        b = tf.reshape(b, [1, 1, nd, ns])
        w = w*b - tf.cast(1e10, w.dtype)*(1-b)
        return w

    def multihead_attn(q, k, v):
        # q, k, v habe shape [batch, heads, sequence, features]
        w = tf.matmul(q, k, transpose_b=True)
        w = w * tf.rsqrt(tf.cast(v.shape[-1].value, w.dtype))

        w = mask_attn_weights(w)
        w = softmax(w)
        a = tf.matmul(w, v)
        return a

    with tf.variable_scope(scope):
        c = conv1d(x, 'c_attn', n_state*3)
        q, k, v = map(split_heads, tf.split(c, 3, axis=2))

        a = multihead_attn(q, k, v)
        a = merge_heads(a)
        a = conv1d(a, 'c_proj', n_state)
        return a


def mlp(x, scope, n_state, *, hparams):
    with tf.variable_scope(scope):
        nx = x.shape[-1].value
        h = gelu(conv1d(x, 'c_fc', n_state))
        h2 = conv1d(h, 'c_proj', nx)
        return h2

def block(x, scope, *, hparams):
    with tf.variable_scope(scope):
        nx = x.shape[-1].value
        a = attn(norm(x, 'ln_1'), 'attn', nx, hparams=hparams)
        x = x + a
        m = mlp(norm(x, 'ln_2'), 'mlp', nx * 4, hparams=hparams)
        x = x + m
        return x

def shape_list(x):
    static = x.shape.as_list()
    dynamic = tf.shape(x)
    return [dynamic[i] if s is None else s for i, s in enumerate(static)]

def expand_tile(value, size):
    value = tf.convert_to_tensor(value, name='value')
    ndims = value.shape.ndims
    return tf.tile(tf.expand_dims(value, axis=0), [size] + [1]*ndims)

def position_for(tokens, past_length):
    batch_size = tf.shape(tokens)[0]
    nsteps = tf.shape(tokens)[1]
    return expand_tile(past_length + tf.range(nsteps), batch_size)

def get_train_ops(hparams, loss, global_step):    
    warmup_steps = hparams.warmup_steps

    # Inverse square root of the timestep proprotional learning rate with warmup
    lr = tf.cond(global_step < warmup_steps, lambda: math.pow(warmup_steps, -1.5), 
        lambda: tf.math.pow(tf.cast(global_step, dtype=tf.float32), -1.5))
    lr = math.pow(hparams.n_embd, -0.5) * tf.cast((1 + global_step), dtype=tf.float32) * lr

    optimizer = tf.train.AdamOptimizer(learning_rate=lr)
    gradients, variables = zip(*optimizer.compute_gradients(loss))
    global_norm = tf.global_norm(gradients)
    if hparams.max_grad_norm:
        gradients, global_norm = tf.clip_by_global_norm(gradients, hparams.max_grad_norm, use_norm=global_norm)

    train_ops = optimizer.apply_gradients(zip(gradients, variables), global_step=global_step, name="training")

    return train_ops, global_norm, lr
