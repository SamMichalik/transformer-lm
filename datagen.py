from concurrent.futures import ThreadPoolExecutor
from subprocess import run, PIPE

import numpy as np

class Dataset():
    def __init__(self, inf, context=512, batch=8, stride=None, buffer=None):
        self.char_to_idx = make_char_to_idx(inf)
        self.idx_to_char = {v: k for k, v in self.char_to_idx.items()}
        self.n_chars = get_char_count(inf)
        self.n_vocab = len(self.char_to_idx)
        self.context = context
        self.batch = batch
        self.stride = stride
        self.buffer = buffer
        self.infile = inf
        if stride is not None:
            self.aprox_n_batches = ((self.n_chars - (context + 1)) // stride + 1) // batch
        else:
            self.aprox_n_batches = ((self.n_chars - (context + 1)) // context + 1) // batch

    def get_iterator(self):
        return data_iterator(self.infile, self.char_to_idx, 
                            self.buffer, 
                            self.context, 
                            self.batch, 
                            self.stride)

def data_iterator(inf, char_to_idx, buffer=65536, context=512, batch=8, stride=8):
    """
        Generator yielding batches of { features, labels }.
        Assumes the language modelling objective.
        A features instance is a `context` long continuous block of characters.
        The corresponding label to an element of such an instance is the immediately 
        following character.
        Example:
            'nekonecno'
            features: char_to_idx('nekonecn')
            labels: char_to_idx('ekonecno')
        Args:
            inf: path to file containing input data
            char_to_idx: mapping from characters to indices
            buffer: the size of the chunk by which the file is read
            context: the size of the model's context window
            batch: batch size
        Returns:
            A generator function yielding batches of the input data.
    """
    inf = open(inf, 'r', encoding='utf-8')
    
    executor = ThreadPoolExecutor(max_workers=1)
    x_it = _task(inf, buffer, context, batch, char_to_idx, stride)
    
    while x_it != None:
        future = executor.submit(_task, inf, buffer, context, batch, char_to_idx, stride)
        yield from x_it()
        x_it = future.result()
    inf.close()

def _task(inf, buffer, context, batch, char_to_idx, stride=None):
    if stride is None:
        stride = context
    
    if buffer is None:
        buf = inf.read()
        buffer = len(buf)
    else:
        buf = inf.read(buffer)

    steps = (buffer - (context + 1)) // stride + 1 # ugly +1 because of labels
    
    # End of file => end of iteration
    if buf == '':
        return None
    
    idx = [char_to_idx[c] for c in buf]
    x = np.ndarray((steps, context), dtype=np.int32)
    y = np.ndarray((steps, context), dtype=np.int32)
    for i, j in enumerate(range(0, len(idx) - (context + 1) + 1, stride)):
        x[i] = idx[j:j + context]
        y[i] = idx[j + 1:j + context + 1]
    
    n_elems = x.shape[0]
    n_elems = (n_elems // batch) * batch # take only as much as can be batched
    x = x[:n_elems]
    y = y[:n_elems]
    total_batches = n_elems // batch
    
    # shuffle elements from the buffer so that batches are more
    # internally independent
    perm = np.random.permutation(total_batches)

    x = np.reshape(a=x, newshape=(-1, batch, context))
    y = np.reshape(a=y, newshape=(-1, batch, context))

    def shuffling_iterator():
        # yields shuffled
        for i in perm:
            yield { 'features': x[i], 'labels': y[i] }

    def iterator():
        for bx, by in zip(x, y):
            yield {'features': bx, 'labels': by}
    
    return shuffling_iterator


def make_char_to_idx(inf):
    char_to_idx = {}
    i = 0
    
    with open(inf, 'r', encoding='utf-8') as inf:
        for l in inf:
            for c in l:
                if c not in char_to_idx:
                    char_to_idx[c] = i
                    i += 1

    return char_to_idx

def get_char_count(inf):
    o = run(['wc', '-m', inf], stdout=PIPE, stderr=PIPE).stdout
    return int(o.decode('utf-8').split(' ')[0])
