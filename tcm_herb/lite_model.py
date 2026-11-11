"""Map this torchvision MobileNetV2's inference weights into an NHWC TF graph.

No training or Keras MobileNet substitution is performed. Explicit symmetric
padding preserves torchvision's stride-2 sampling, unlike TF SAME padding.
TensorFlow is imported lazily so normal training does not require it.
"""

from collections import Counter
import hashlib

import numpy as np
from torch import nn
from torchvision.models.mobilenetv2 import InvertedResidual


def folded_weights(conv, norm):
    """Fold an eval BatchNorm into its preceding convolution in float32."""
    if norm.training or not norm.track_running_stats:
        raise ValueError("Only inference BatchNorm with running statistics is supported.")
    weight = conv.weight.detach().cpu().numpy().copy()
    bias = np.zeros(conv.out_channels, np.float32) if conv.bias is None else conv.bias.detach().cpu().numpy().copy()
    gamma = norm.weight.detach().cpu().numpy() if norm.affine else np.ones_like(bias)
    beta = norm.bias.detach().cpu().numpy() if norm.affine else np.zeros_like(bias)
    mean, variance = norm.running_mean.cpu().numpy(), norm.running_var.cpu().numpy()
    factor = gamma / np.sqrt(variance + norm.eps)
    return weight * factor[:, None, None, None], (bias - mean) * factor + beta


def build_tf_function(model, image_size):
    import tensorflow as tf

    if model.training:
        raise ValueError("The source model must be in eval mode.")
    mapping = []

    def convolution(layer, name, norm=None):
        if layer.padding_mode != "zeros" or layer.dilation != (1, 1):
            raise ValueError(f"Unsupported convolution configuration: {name}")
        if norm is not None:
            weight, bias = folded_weights(layer, norm)
        else:
            weight = layer.weight.detach().cpu().numpy()
            bias = None if layer.bias is None else layer.bias.detach().cpu().numpy()
        depthwise = layer.groups == layer.in_channels and layer.groups != 1
        if depthwise:
            multiplier = layer.out_channels // layer.in_channels
            kernel = weight.reshape(layer.in_channels, multiplier, *layer.kernel_size).transpose(2, 3, 0, 1)
        elif layer.groups == 1:
            kernel = weight.transpose(2, 3, 1, 0)
        else:
            raise ValueError(f"Unsupported grouped convolution: {name}")
        kernel = tf.constant(np.ascontiguousarray(kernel), dtype=tf.float32, name=name + "_weight")
        bias_tensor = None if bias is None else tf.constant(bias, dtype=tf.float32, name=name + "_bias")
        stride = [1, *layer.stride, 1]
        ph, pw = layer.padding
        mapping.append({"source": name, "operation": "DEPTHWISE_CONV_2D" if depthwise else "CONV_2D",
                        "folded_batch_norm": norm is not None, "padding_hw": [ph, pw], "stride_hw": list(layer.stride)})

        def apply(inputs):
            if ph or pw:
                inputs = tf.pad(inputs, [[0, 0], [ph, ph], [pw, pw], [0, 0]], name=name + "_pad")
            if depthwise:
                value = tf.nn.depthwise_conv2d(inputs, kernel, strides=stride, padding="VALID", name=name)
            else:
                value = tf.nn.conv2d(inputs, kernel, strides=stride, padding="VALID", name=name)
            return tf.nn.bias_add(value, bias_tensor) if bias_tensor is not None else value
        return apply

    def compile_layer(layer, name):
        if isinstance(layer, InvertedResidual):
            branch = compile_layer(layer.conv, name + "_conv")
            if layer.use_res_connect:
                return lambda inputs: tf.add(inputs, branch(inputs), name=name + "_residual")
            return branch
        if isinstance(layer, nn.Sequential):
            children, operations, index = list(layer.children()), [], 0
            while index < len(children):
                child = children[index]
                child_name = f"{name}_{index}"
                if isinstance(child, nn.Conv2d) and index + 1 < len(children) and isinstance(children[index + 1], nn.BatchNorm2d):
                    operations.append(convolution(child, child_name, children[index + 1]))
                    index += 2
                else:
                    operations.append(compile_layer(child, child_name))
                    index += 1

            def apply(inputs):
                for operation in operations:
                    inputs = operation(inputs)
                return inputs
            return apply
        if isinstance(layer, nn.Conv2d):
            return convolution(layer, name)
        if isinstance(layer, nn.ReLU6):
            return lambda inputs: tf.nn.relu6(inputs, name=name)
        if isinstance(layer, nn.Linear):
            weight = tf.constant(layer.weight.detach().cpu().numpy().T.copy(), dtype=tf.float32, name=name + "_weight")
            bias = tf.constant(layer.bias.detach().cpu().numpy(), dtype=tf.float32, name=name + "_bias")
            mapping.append({"source": name, "operation": "FULLY_CONNECTED"})
            return lambda inputs: tf.nn.bias_add(tf.matmul(inputs, weight), bias, name=name)
        if isinstance(layer, (nn.Dropout, nn.Identity)):
            if layer.training:
                raise ValueError(f"Training-mode layer: {name}")
            return lambda inputs: inputs
        raise ValueError(f"Unsupported source layer: {name}: {type(layer).__name__}")

    features = compile_layer(model.features, "features")
    classifier = compile_layer(model.classifier, "classifier")

    @tf.function(input_signature=[tf.TensorSpec([1, image_size, image_size, 3], tf.float32, name="image")], autograph=False)
    def forward(inputs):
        pooled = tf.reduce_mean(features(inputs), axis=[1, 2], name="global_average_pool")
        return tf.identity(classifier(pooled), name="logits")

    concrete = forward.get_concrete_function()
    return concrete, mapping


def quantize_input(values, scale, zero_point):
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("Input scale must be finite and positive.")
    # Clip before casting: a direct int8 cast would wrap values outside its range.
    return np.clip(np.rint(values / scale) + zero_point, -128, 127).astype(np.int8)


class LitePredictor:
    """Fixed batch-1 interpreter using native CPU builtins, without delegates."""

    def __init__(self, path, threads=4):
        import tensorflow as tf

        self.interpreter = tf.lite.Interpreter(
            model_path=str(path), num_threads=threads,
            experimental_op_resolver_type=tf.lite.experimental.OpResolverType.BUILTIN_WITHOUT_DEFAULT_DELEGATES,
        )
        self.interpreter.allocate_tensors()
        inputs, outputs = self.interpreter.get_input_details(), self.interpreter.get_output_details()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("Expected one input and one output.")
        self.input, self.output = inputs[0], outputs[0]
        if self.input["dtype"] not in (np.float32, np.int8) or self.output["dtype"] != self.input["dtype"]:
            raise ValueError("Expected matching FP32 or INT8 input/output types.")

    def __call__(self, values):
        if tuple(values.shape) != tuple(self.input["shape"]) or not np.isfinite(values).all():
            raise ValueError("Invalid input shape or non-finite values.")
        if self.input["dtype"] == np.int8:
            scale, zero_point = self.input["quantization"]
            inputs = quantize_input(values, scale, zero_point)
        else:
            inputs = values.astype(np.float32, copy=False)
        self.interpreter.set_tensor(self.input["index"], inputs)
        self.interpreter.invoke()
        raw = self.interpreter.get_tensor(self.output["index"])
        if self.output["dtype"] == np.int8:
            scale, zero_point = self.output["quantization"]
            if scale <= 0:
                raise ValueError("Invalid output quantization scale.")
            return (raw.astype(np.float32) - zero_point) * scale, raw
        return raw, raw


def inspect_model(path, require_int8=False):
    """Inspect the FlatBuffer itself, including all tensors and operator types."""
    from tensorflow.lite.python import schema_py_generated as schema

    blob = path.read_bytes()
    model = schema.Model.GetRootAsModel(blob, 0)
    op_names = {value: name for name, value in vars(schema.BuiltinOperator).items() if isinstance(value, int)}
    type_names = {value: name for name, value in vars(schema.TensorType).items() if isinstance(value, int)}
    operations, tensors, float_tensors, interfaces, channels = Counter(), Counter(), [], [], []
    for graph_index in range(model.SubgraphsLength()):
        graph = model.Subgraphs(graph_index)
        for index in range(graph.TensorsLength()):
            tensor = graph.Tensors(index)
            dtype = type_names[tensor.Type()]
            tensors[dtype] += 1
            if dtype.startswith("FLOAT") or dtype.startswith("BFLOAT"):
                float_tensors.append(tensor.Name().decode())
        for index in range(graph.OperatorsLength()):
            operator = graph.Operators(index)
            code = model.OperatorCodes(operator.OpcodeIndex())
            operation = op_names[max(code.BuiltinCode(), code.DeprecatedBuiltinCode())]
            if operation == "CUSTOM":
                raise ValueError(f"Custom/Flex operator is not allowed: {code.CustomCode()}")
            operations[f"{operation}:v{code.Version()}"] += 1
            if require_int8 and operation in {"CONV_2D", "DEPTHWISE_CONV_2D", "FULLY_CONNECTED"}:
                weight = graph.Tensors(operator.Inputs(1))
                if type_names[weight.Type()] != "INT8":
                    raise ValueError(f"Non-int8 weights in {operation}")
                quant = weight.Quantization()
                channels.append({"operation": operation, "scales": quant.ScaleLength(), "axis": quant.QuantizedDimension()})
        io = {}
        for kind in ("Inputs", "Outputs"):
            getter, count = getattr(graph, kind), getattr(graph, kind + "Length")()
            io[kind.lower()] = []
            for index in range(count):
                tensor = graph.Tensors(getter(index))
                quant = tensor.Quantization()
                item = {"name": tensor.Name().decode(), "dtype": type_names[tensor.Type()],
                        "shape": tensor.ShapeAsNumpy().tolist()}
                if quant is not None:
                    item.update(scales=quant.ScaleAsNumpy().tolist() if quant.ScaleLength() else [],
                                zero_points=quant.ZeroPointAsNumpy().tolist() if quant.ZeroPointLength() else [])
                if require_int8 and item["dtype"] != "INT8":
                    raise ValueError("INT8 input/output contract was not preserved.")
                io[kind.lower()].append(item)
        interfaces.append(io)
    if require_int8 and float_tensors:
        raise ValueError(f"Floating-point tensors remain: {float_tensors}")
    return {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
            "operators": dict(operations), "tensor_types": dict(tensors), "floating_tensors": float_tensors,
            "interfaces": interfaces, "weight_quantization": channels, "full_integer_verified": require_int8}
