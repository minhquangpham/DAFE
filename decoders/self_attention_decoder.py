"""Define self-attention decoder."""

import tensorflow as tf

from opennmt.decoders.decoder import Decoder
from opennmt.decoders.self_attention_decoder import SelfAttentionDecoder
from layers import common, transformer
from opennmt.layers.position import SinusoidalPositionEncoder
from layers.layers import Multi_domain_FeedForwardNetwork
from utils.utils_ import make_domain_mask
class Multi_domain_SelfAttentionDecoder(Decoder):
  
  def __init__(self,
               num_layers,
               num_domains,
               num_domain_units=128,
               num_units=512,
               num_heads=8,
               ffn_inner_dim=2048,
               dropout=0.1,
               attention_dropout=0.1,
               ffn_dropout=0.1,
               ffn_activation=tf.nn.relu,
               position_encoder_class=SinusoidalPositionEncoder,
               num_sources=1,
               **kwargs):
    
    super(Multi_domain_SelfAttentionDecoder, self).__init__(num_sources=num_sources, **kwargs)
    self.num_units = num_units
    self.num_heads = num_heads
    self.dropout = dropout
    self.position_encoder = None
    if position_encoder_class is not None:
      self.position_encoder = position_encoder_class()
    self.layer_norm = common.LayerNorm()
    self.layers = [
        transformer.SelfAttentionDecoderLayer(
            self.num_units,
            self.num_heads,
            ffn_inner_dim,
            num_sources=num_sources,
            dropout=dropout,
            attention_dropout=attention_dropout,
            ffn_dropout=ffn_dropout,
            ffn_activation=ffn_activation)
        for i in range(num_layers)]
    self.mask = make_domain_mask(num_domains, num_domain_units=num_domain_units)
    self.multi_domain_layers = [
        Multi_domain_FeedForwardNetwork(num_domains*num_domain_units, num_units, name="ADAP_%d"%i)
        for i in range(num_layers)]

  def initialize(self, vocab_size=None, output_layer=None):
    
    if output_layer is not None:
      self.output_layer = output_layer
    else:
      if vocab_size is None:
        raise ValueError("One of vocab_size and output_layer must be set")
      self.output_layer = common.Dense(vocab_size)

  @property
  def minimum_sources(self):
    return 0

  @property
  def maximum_sources(self):
    return 1e6  # An arbitrary large number.

  @property
  def support_alignment_history(self):
    return self.num_sources == 1

  def map_v1_weights(self, weights):
    m = []
    m += self.output_layer.map_v1_weights(weights["dense"])
    m += self.layer_norm.map_v1_weights(weights["LayerNorm"])
    for i, layer in enumerate(self.layers):
      m += layer.map_v1_weights(weights["layer_%d" % i])
    return m

  def _run(self,
           inputs,
           sequence_length=None,
           cache=None,
           memory=None,
           memory_sequence_length=None,
           step=None,
           training=None):
    # Process inputs.
    domain = inputs[1]
    domain_mask = tf.nn.embedding_lookup(self.mask, domain)
    inputs = inputs[0]
    inputs *= self.num_units**0.5
    if self.position_encoder is not None:
      inputs = self.position_encoder(inputs, position=step + 1 if step is not None else None)
    inputs = common.dropout(inputs, self.dropout, training=training)

    # Prepare query mask.
    mask = None
    if step is None:
      maximum_length = tf.shape(inputs)[1]
      if sequence_length is None:
        batch_size = tf.shape(inputs)[0]
        sequence_length = tf.fill([batch_size], maximum_length)
      mask = transformer.future_mask(sequence_length, maximum_length=maximum_length)

    # Prepare memory mask.
    memory_mask = None
    if memory is not None:
      if not isinstance(memory, (list, tuple)):
        memory = (memory,)
    if memory_sequence_length is not None:
      if not isinstance(memory_sequence_length, (list, tuple)):
        memory_sequence_length = (memory_sequence_length,)
      memory_mask = [
          tf.sequence_mask(mem_length, maxlen=tf.shape(mem)[1])
          for mem, mem_length in zip(memory, memory_sequence_length)]

    # Run each layer.
    new_cache = []
    for i, (layer, multi_domain_layer) in enumerate(zip(self.layers,self.multi_domain_layers)):

      inputs, layer_cache, attention = layer(
          inputs,
          mask=mask,
          memory=memory,
          memory_mask=memory_mask,
          cache=cache[i] if cache is not None else None,
          training=training)
      new_cache.append(layer_cache)
      #print("inputs_%d"%i,inputs)
      inputs = multi_domain_layer(inputs, domain_mask) + inputs

    outputs = self.layer_norm(inputs)
    return outputs, new_cache, attention

  def forward(self,
              inputs,
              sequence_length=None,
              initial_state=None,
              memory=None,
              memory_sequence_length=None,
              input_fn=None,
              sampling_probability=None,
              training=None):
    _ = initial_state
    _ = input_fn
    if sampling_probability is not None:
      raise ValueError("Scheduled sampling is not supported by this decoder")
    outputs, state, attention = self._run(
        inputs,
        sequence_length=sequence_length,
        memory=memory,
        memory_sequence_length=memory_sequence_length,
        training=training)
    logits = self.output_layer(outputs)
    return logits, state, attention
    
  def forward_fn(self,
              inputs,
              args_dict,
              sequence_length=None,
              initial_state=None,
              memory=None,
              memory_sequence_length=None,
              input_fn=None,
              sampling_probability=None,
              training=None):
    _ = initial_state
    _ = input_fn
    if sampling_probability is not None:
      raise ValueError("Scheduled sampling is not supported by this decoder")
    outputs, state, attention = self._run_forward_fn(
        inputs,
        args_dict,
        sequence_length=sequence_length,
        memory=memory,
        memory_sequence_length=memory_sequence_length,
        training=training)
    logits = self.output_layer.forward_fn(outputs, args_dict)
    return logits, state, attention

  def step(self,
           inputs,
           timestep,
           state=None,
           memory=None,
           memory_sequence_length=None,
           training=None):
    
    inputs = [tf.expand_dims(inputs[0], 1), inputs[1]]
    outputs, state, attention = self._run(
        inputs,
        cache=state,
        memory=memory,
        memory_sequence_length=memory_sequence_length,
        step=timestep,
        training=training)
    outputs = tf.squeeze(outputs, axis=1)
    if attention is not None:
      attention = tf.squeeze(attention, axis=1)
    return outputs, state, attention
    
  def _get_initial_state(self, batch_size, dtype, initial_state=None):

    # The decoder state contains the keys and values projections of the previous timesteps.
    _ = initial_state
    cache = []
    for _ in self.layers:
      shape = [batch_size, self.num_heads, 0, self.num_units // self.num_heads]
      self_kv = (tf.zeros(shape, dtype=dtype), tf.zeros(shape, dtype=dtype))
      memory_kv = [
          (tf.zeros(shape, dtype=dtype), tf.zeros(shape, dtype=dtype))
          for _ in range(self.num_sources)]
      cache.append(dict(self_kv=self_kv, memory_kv=memory_kv))
    return cache

  def _run_forward_fn(self,
           inputs,
           args_dict,
           sequence_length=None,
           cache=None,
           memory=None,
           memory_sequence_length=None,
           step=None,
           training=None):
    domain = inputs[1]
    domain_mask = tf.nn.embedding_lookup(self.mask, domain)
    inputs = inputs[0]
    inputs *= self.num_units**0.5
    if self.position_encoder is not None:
      inputs = self.position_encoder(inputs, position=step + 1 if step is not None else None)
    inputs = common.dropout(inputs, self.dropout, training=training)

    # Prepare query mask.
    mask = None
    if step is None:
      maximum_length = tf.shape(inputs)[1]
      if sequence_length is None:
        batch_size = tf.shape(inputs)[0]
        sequence_length = tf.fill([batch_size], maximum_length)
      mask = transformer.future_mask(sequence_length, maximum_length=maximum_length)

    # Prepare memory mask.
    memory_mask = None
    if memory is not None:
      if not isinstance(memory, (list, tuple)):
        memory = (memory,)
    if memory_sequence_length is not None:
      if not isinstance(memory_sequence_length, (list, tuple)):
        memory_sequence_length = (memory_sequence_length,)
      memory_mask = [
          tf.sequence_mask(mem_length, maxlen=tf.shape(mem)[1])
          for mem, mem_length in zip(memory, memory_sequence_length)]
    
    # Run each layer.
    new_cache = []
    for i, (layer, multi_domain_layer) in enumerate(zip(self.layers,self.multi_domain_layers)):

      inputs, layer_cache, attention = layer.forward_fn(
          inputs,
          args_dict,
          mask=mask,          
          memory=memory,
          memory_mask=memory_mask,
          cache=cache[i] if cache is not None else None,
          training=training)
      new_cache.append(layer_cache)
      #print("inputs_%d"%i,inputs)
      inputs = multi_domain_layer.forward_fn(inputs, args_dict, domain_mask) + inputs

    outputs = self.layer_norm.forward_fn(inputs, args_dict)
    return outputs, new_cache, attention