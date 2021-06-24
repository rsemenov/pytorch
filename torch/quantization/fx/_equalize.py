import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.fx import GraphModule
from torch.fx.graph import Node

from .utils import (
    WEIGHT_INDEX_DICT,
    get_new_attr_name_with_prefix,
    maybe_get_next_module,
    _parent_name,
)
from ..observer import (
    PerChannelMinMaxObserver,
    _with_args,
    ObserverBase,
)
from ..utils import check_min_max_valid

from collections import namedtuple
from typing import Dict, Any, Tuple, Optional
import warnings


class _InputEqualizationObserver(nn.Module):
    r"""Observer for tracking the running min/max values of input columns, and
    computing the quantization parameters for the overall min/max input values.

    Args:
        dtype: Quantized data type
        qscheme: Quantization scheme
        quant_min: Minimum quantization value. If unspecified, it will
            follow the 8-bit setup.
        quant_max: Maximum quantization value. If unspecified, it will
            follow the 8-bit setup.

    The running minimum/maximum :math:`x_\text{min/max}` are computed in the
    same way as :class:`~torch.quantization.observer.PerChannelMinMaxObserver`,
    with the difference that the running min/max values are stored per column.
    This observer is intended to be used along with a WeightEqualizationObserver
    to calculate the equalization scale.
    """

    def __init__(self, dtype=torch.quint8, qscheme=torch.per_tensor_affine,
                 quant_min=None, quant_max=None, factory_kwargs=None) -> None:
        super(_InputEqualizationObserver, self).__init__()

        if qscheme not in {torch.per_tensor_affine, torch.per_tensor_symmetric}:
            raise TypeError("Input qscheme must be per-tensor")

        self.dtype = dtype
        self.qscheme = qscheme

        self.input_obs = PerChannelMinMaxObserver(ch_axis=1, dtype=dtype,
                                                  qscheme=qscheme,
                                                  quant_min=quant_min,
                                                  quant_max=quant_max,
                                                  factory_kwargs=factory_kwargs)

        self.equalization_scale = torch.empty(0)

    def forward(self, x_orig):
        # TODO: Allow for convoluational layers
        if not (x_orig.ndim == 2):
            raise ValueError("InputEqualizationObserver only supports Linear layers")

        return self.input_obs(x_orig)

    def get_input_minmax(self):
        return (self.input_obs.min_vals, self.input_obs.max_vals)

    def set_equalization_scale(self, equalization_scale):
        self.equalization_scale = equalization_scale

    def calculate_scaled_minmax(self):
        r""" Returns the scaled min/max inputs
        """
        if self.equalization_scale.nelement() == 0:
            warnings.warn(
                "Must call calculate_scale before calling calculate_qparams.\
                Returning default min and max input."
            )
            return torch.tensor([0]), torch.tensor([0])

        # Calculate qparams for the scaled min/max inputs
        # Scale the input by the equalization scale located at the same column
        # index
        (min_inputs, max_inputs) = self.get_input_minmax()
        min_input_scaled = torch.min(torch.mul(min_inputs, self.equalization_scale))
        max_input_scaled = torch.max(torch.mul(max_inputs, self.equalization_scale))

        return min_input_scaled, max_input_scaled

    with_args = classmethod(_with_args)


class _WeightEqualizationObserver(nn.Module):
    r"""Observer for tracking the running min/max values of weight columns and
    rows, and computing the quantization parameters for the weight rows.

    Args:
        dtype: Quantized data type
        qscheme: Quantization scheme
        quant_min: Minimum quantization value. If unspecified, it will
            follow the 8-bit setup.
        quant_max: Maximum quantization value. If unspecified, it will
            follow the 8-bit setup.

    This observer is made up of 1 PerChannelMinMaxObserver `weight_col_obs` used
    to record the running minimum and maximum of columns of incoming weight
    tensors. This observer is intended to be used along with an
    InputEqualizationObserver to calculate the equalization scale.

    The running minimum/maximum :math:`w_\text{min/max}` are computed in the
    same way as :class:`~torch.quantization.observer.PerChannelMinMaxObserver`.
    """

    def __init__(self, dtype=torch.qint8, qscheme=torch.per_tensor_affine, quant_min=None,
                 quant_max=None, factory_kwargs=None) -> None:
        super(_WeightEqualizationObserver, self).__init__()

        self.dtype = dtype
        self.qscheme = qscheme
        self.ch_axis = 1

        self.weight_col_obs = PerChannelMinMaxObserver(ch_axis=1, dtype=dtype,
                                                       qscheme=qscheme,
                                                       quant_min=quant_min,
                                                       quant_max=quant_max,
                                                       factory_kwargs=factory_kwargs)

        self.equalization_scale = torch.empty(0)

    def forward(self, w_orig):
        # TODO: Allow for convoluational layers
        if not (w_orig.ndim == 2):
            raise ValueError("WeightEqualizationObserver only supports Linear layers")
        return self.weight_col_obs(w_orig)

    def get_weight_col_minmax(self):
        return (self.weight_col_obs.min_vals, self.weight_col_obs.max_vals)

    def set_equalization_scale(self, equalization_scale):
        self.equalization_scale = equalization_scale

    with_args = classmethod(_with_args)


def calculate_equalization_scale(input_obs: _InputEqualizationObserver,
                                 weight_obs: _WeightEqualizationObserver) -> torch.Tensor:
    r""" Calculates the equalization scale and sets the equalization_scale value
    in the observers.

    Args:
        input_obs: Observer that tracks the ranges for the input columns
        weight_obs: Observer that tracks the ranges for the weight columns
    """

    (min_inputs, max_inputs) = input_obs.get_input_minmax()
    (min_weights, max_weights) = weight_obs.get_weight_col_minmax()

    if not (check_min_max_valid(min_inputs, max_inputs) and check_min_max_valid(min_weights, max_weights)):
        return torch.tensor(1)

    if not (min_inputs.shape == min_weights.shape):
        raise ValueError(
            "Input and Weight must have the same column dimension. " +
            f"Found {min_inputs.shape} and {max_inputs.shape} instead."
        )

    equalization_scale = torch.sqrt((max_weights - min_weights) / (max_inputs - min_inputs))
    return equalization_scale


class EqualizationQConfig(namedtuple('EqualizationQConfig', ['input_activation', 'weight'])):
    """
    Describes how to quantize a layer or a part of the network specifically for
    input-weight equalization by providing settings (observer classes) for
    inputs, outputs, and weights.

    Note that EqualizationQConfig needs to contain observer **classes** (like
    MinMaxObserver) or a callable that returns instances on invocation, not the
    concrete observer instances themselves.
    Quantization function will instantiate observers multiple times for each of
    the layers.

    Observer classes have usually reasonable default arguments, but they can be
    overwritten with `with_args` method (that behaves like functools.partial):

    my_qconfig = EqualizationQConfig(input_activation=_InputEqualizationObserver.with_args(dtype=torch.qint8),
                                    weight=_WeightEqualizationObserver.with_args(dtype=torch.qint8))
    """
    def __new__(cls, input_activation=torch.nn.Identity, weight=torch.nn.Identity):
        if isinstance(input_activation, nn.Module) or isinstance(weight, nn.Module):
            raise ValueError("EqualizationQConfig received observer instance, please pass observer class instead. " +
                             "Use MyObserver.with_args(x=1) to override arguments to constructor if needed")
        self = super(EqualizationQConfig, cls).__new__(cls, input_activation, weight)
        return self


input_equalization_observer = _InputEqualizationObserver.with_args(
    dtype=torch.quint8, qscheme=torch.per_tensor_symmetric)
weight_equalization_observer = _WeightEqualizationObserver.with_args(
    dtype=torch.qint8, qscheme=torch.per_channel_symmetric)
default_equalization_qconfig = EqualizationQConfig(input_activation=input_equalization_observer,
                                                   weight=weight_equalization_observer)

def node_supports_equalization(node: Node, modules) -> bool:
    """ Checks if the current node supports equalization
    Currently we only support nn.Linear and F.Linear layers
    """
    if node.op == 'call_module':
        return isinstance(modules[node.target], nn.Linear)
    elif node.op == 'call_function':
        return node.target == F.linear
    return False

def is_equalization_observer(observer: nn.Module) -> bool:
    return (isinstance(observer, _InputEqualizationObserver) or
            isinstance(observer, _WeightEqualizationObserver))

def get_op_node_and_weight_eq_obs(
    input_eq_obs_node: Node,
    model: GraphModule,
    modules: Dict[str, nn.Module]
) -> Tuple[Optional[Node], Optional[_WeightEqualizationObserver]]:
    """ Gets the following weight equalization observer. There should always
    exist a weight equalization observer after an input equalization observer.

    Returns the operation node that follows the input equalizatoin observer node
    and the weight equalization observer
    """

    # Find the op node that comes directly after the input equaliation observer
    op_node = None
    for user in input_eq_obs_node.users.keys():
        if node_supports_equalization(user, modules):
            op_node = user
            break

    assert(op_node is not None)
    if op_node.op == 'call_module':
        # If the op_node is a nn.Linear layer, then it must have a
        # WeightEqualizationObserver configuration
        equalization_qconfig_map: Dict[str, Any] = model._equalization_qconfig_map  # type: ignore[assignment]
        assert(equalization_qconfig_map.get(op_node.name, None) is not None)
        weight_eq_obs = equalization_qconfig_map.get(op_node.name, None).weight()

        assert(isinstance(weight_eq_obs, _WeightEqualizationObserver))
        return op_node, weight_eq_obs

    elif op_node.op == 'call_function':
        weight_node = maybe_get_functional_weight_node(op_node, modules, 'eq_obs')
        if weight_node is not None:
            weight_eq_obs = modules[str(weight_node.target)]
            assert(isinstance(weight_eq_obs, _WeightEqualizationObserver))
            return op_node, weight_eq_obs

    return None, None

def maybe_get_functional_weight_node(op_node: Node, modules: Dict[str, nn.Module], attr: str) -> Optional[Node]:
    """ Retrieves the weight node with the given attribute that is input into
    the given op_node.

    The path should look something like: get_attr(weight) -> weight_quant_obs -> weight_eq_obs -> op_node
    So we can trace back a specific number of steps to find the node with the
    given attribute.

    Args:
        op_node: A functional node
        attr: Can be one of {'eq_obs', 'quant_obs', 'get_attr'}. Specifies the
              node we are looking for which will be used to find the number of
              steps to take from the op_node before returning the node
    """
    WEIGHT_ATTR_INDEX_DICT = {'eq_obs': 1, 'quant_obs': 2, 'get_attr': 3}

    # Get the weight equalization observer
    node = None
    assert(op_node.op == 'call_function' and op_node.target in WEIGHT_INDEX_DICT)
    for i, node_arg in enumerate(op_node.args):
        if i in WEIGHT_INDEX_DICT[op_node.target]:  # type: ignore[index]
            node = node_arg
            break
    if node is None:
        raise LookupError('Could not find the equalization observer')

    assert(isinstance(node, Node) and node.op == 'call_module' and
           isinstance(modules[str(node.target)], _WeightEqualizationObserver))

    # Step back from the weight equalization observer a specific number of steps
    index = WEIGHT_ATTR_INDEX_DICT.get(attr, 0)
    for _ in range(index - 1):
        node = node.args[0]
        if node is None:
            raise LookupError(f'Could not find the node {index} steps away from ' +
                              f'the op node with attr {attr}.')
        assert(isinstance(node, Node))

    return node

def maybe_get_next_input_eq_obs(node: Node, modules: Dict[str, nn.Module]) -> Optional[_InputEqualizationObserver]:
    """ Gets the following input equalization observer if it exists.

    For example, in the case of connecting linear layers:
        x -> inp_obs1 -> eq_obs1 -> linear1 -> out_obs1 -> eq_obs2 -> linear2 -> out_obs2
    If the node being passed in is the linear1 node, then we want to return eq_obs2,
    the following equalization observer for linear2.

    However, if there are no connecting layers:
        x -> inp_obs1 -> eq_obs1 -> linear1 -> out_obs1 -> add
    Then we want to return None.
    """

    assert(node_supports_equalization(node, modules))

    # Locate the following output observer if it exists
    maybe_obs_node = maybe_get_next_module(node, modules, ObserverBase)
    if maybe_obs_node is None:
        return None

    maybe_eq_obs_node = maybe_get_next_module(maybe_obs_node, modules, _InputEqualizationObserver)
    if maybe_eq_obs_node is None:
        return None

    maybe_eq_obs = modules[str(maybe_eq_obs_node)]
    assert(isinstance(maybe_eq_obs, _InputEqualizationObserver))
    return maybe_eq_obs

def maybe_get_next_equalization_scale(node: Node, modules: Dict[str, nn.Module]) -> Optional[torch.Tensor]:
    """ If the next next node is an InputEqualizationObserver then we want to
    return its equalization scale, else we return 1

    This is used in the case where there are two connecting linear layers:
        linear1 -> LinearOutObs -> InputEqObs -> linear2
    In this case, the node given is linear1 and we want to locate the InputEqObs.
    """
    next_inp_eq_obs = maybe_get_next_input_eq_obs(node, modules)
    if next_inp_eq_obs:
        return next_inp_eq_obs.equalization_scale
    return None

def scale_input_observer(node: Node, modules: Dict[str, nn.Module]) -> None:
    """ Scales the following input quantization observer's min/max values by
    updating the values with the scaled min/max values calculated by the input
    equalization observer
    """
    input_eq_obs = modules[str(node.target)]
    assert(isinstance(input_eq_obs, _InputEqualizationObserver))

    input_quant_obs_node = node.args[0]
    assert(isinstance(input_quant_obs_node, Node))

    input_quant_obs = modules[str(input_quant_obs_node.target)]
    if not isinstance(input_quant_obs, ObserverBase):
        return

    min_input_scaled, max_input_scaled = input_eq_obs.calculate_scaled_minmax()
    input_quant_obs.min_val = min_input_scaled
    input_quant_obs.max_val = max_input_scaled

def scale_weight_node(
    node: Node,
    modules: Dict[str, nn.Module],
    equalization_scale: torch.Tensor,
    next_equalization_scale: Optional[torch.Tensor],
) -> None:
    """ Scale the weights for input-weight equalization by multiplying the
    weight by 1/equalization_scale and next_equalization_scale

    Args:
        node: Current node whose weights we want to scale
        equalization_scale: Current node's calculated equalization scale
        next_equalization_scale: Next node's calculated equalization scale if
           the following node needs to be equalized, 1 otherwise
    """
    assert(isinstance(node.target, str))

    # Scale the weights for input-weight equalization
    # If the following layer needs to be equalized then we will multiply its scale
    weight = modules[node.target].weight
    assert(isinstance(weight, torch.Tensor))

    scaled_weight = torch.mul(weight, torch.reciprocal(equalization_scale))

    if next_equalization_scale is None:
        modules[node.target].weight = nn.Parameter(scaled_weight)
        return

    # Multiply the weights row wise by the next equalization scale
    new_shape = [1] * weight.ndim
    new_shape[0] = weight.size(0)
    scaled_weight = torch.mul(scaled_weight, next_equalization_scale.view(new_shape))

    modules[node.target].weight = nn.Parameter(scaled_weight)

    # Multiply the bias element wise by the next equalization scale
    bias = modules[node.target].bias
    assert(isinstance(bias, torch.Tensor))

    scaled_bias = torch.mul(bias, next_equalization_scale)
    modules[node.target].bias = nn.Parameter(scaled_bias)

def scale_weight_functional(
    op_node: Node,
    model: GraphModule,
    modules: Dict[str, nn.Module],
    equalization_scale: torch.Tensor,
    next_equalization_scale: Optional[torch.Tensor],
) -> None:
    """ Scales the weight value for functional layers
    """

    # Get the get_attr(weight) node
    weight_node = maybe_get_functional_weight_node(op_node, modules, 'get_attr')
    if weight_node is None:
        return

    assert(weight_node.op == 'get_attr')
    weight_parent_name, weight_name = _parent_name(weight_node.target)
    weight = getattr(modules[weight_parent_name], weight_name)

    # Scale the weights for input-weight equalization
    # If the following layer needs to be equalized then we will multiply its scale
    scaled_weight = torch.mul(weight, torch.reciprocal(equalization_scale))

    if next_equalization_scale is None:
        setattr(modules[weight_parent_name], weight_name, scaled_weight)
        return

    # Multiply the weights row wise by the next equalization scale
    new_shape = [1] * weight.ndim
    new_shape[0] = weight.size(0)
    scaled_weight = torch.mul(scaled_weight, next_equalization_scale.view(new_shape))

    setattr(modules[weight_parent_name], weight_name, scaled_weight)
    assert(torch.allclose(model.get_buffer(str(weight_node.target)), scaled_weight))

    # Multiply the bias element wise by the next equalization scale
    bias_node = None
    for node, _ in op_node.users.items():
        # Find the node containing the weight values
        if node.op == 'get_attr' and 'bias' in node.name:
            bias_node = node
            break
    if bias_node is None:
        return

    bias_parent_name, bias_name = _parent_name(bias_node.target)
    bias = getattr(modules[bias_parent_name], bias_name)

    scaled_bias = torch.mul(bias, next_equalization_scale)
    setattr(modules[bias_parent_name], bias_name, scaled_bias)

def clear_weight_quant_obs_node(op_node: Node, modules: Dict[str, nn.Module]) -> None:
    """ Given the operation node, we want find the corresponding quantization
    observer and reset its min/max values
    """
    weight_quant_obs_node = maybe_get_functional_weight_node(op_node, modules, 'quant_obs')
    if weight_quant_obs_node is None:
        return

    weight_quant_obs = modules[str(weight_quant_obs_node.target)]
    assert(isinstance(modules[str(weight_quant_obs_node.target)], ObserverBase))
    weight_quant_obs.min_val = torch.tensor(float("inf"))
    weight_quant_obs.max_val = torch.tensor(float("-inf"))

def remove_node(model: GraphModule, node: Node, prev_node: Node):
    """ Removes the given node from the model by replacing all of its users with
    the given previous node
    """
    # For all of the current node's users, replace the current node with
    # the input quantization observer node
    orig_users = list(node.users.keys())
    for user_node in orig_users:
        user_node.replace_input_with(node, prev_node)

    # Erase the InputEqualizationObserver node
    model.graph.erase_node(node)

def update_obs_for_equalization(model: GraphModule, modules: Dict[str, nn.Module]) -> Dict[str, _WeightEqualizationObserver]:
    """ Update all of the observer's equalization scale. For each
    InputEqualizationObserver, we will find the location of the next
    WeightEqualizationObserver, create it, and calculate the equalization scale
    based on the two observers.

    We will then return a dictionary mapping operation node names to
    the corresponding WeightEqualizationObservers for that operation.
    """
    weight_eq_obs_dict = {}
    for node in model.graph.nodes:
        if node.op == 'call_module' and isinstance(modules[node.target], _InputEqualizationObserver):
            input_eq_obs = modules[node.target]
            assert(isinstance(input_eq_obs, _InputEqualizationObserver))
            op_node, weight_eq_obs = get_op_node_and_weight_eq_obs(node, model, modules)

            if op_node is None or weight_eq_obs is None:
                continue

            if op_node.op == 'call_module':
                # Calibrate the weight equalization observer since it has just
                # been created
                weight_eq_obs(modules[str(op_node.target)].weight)

            # Calculate and set the equalization scale values
            equalization_scale = calculate_equalization_scale(input_eq_obs, weight_eq_obs)
            input_eq_obs.set_equalization_scale(equalization_scale)
            weight_eq_obs.set_equalization_scale(equalization_scale)

            weight_eq_obs_dict[op_node.name] = weight_eq_obs

    return weight_eq_obs_dict

def convert_eq_obs(
    model: GraphModule,
    modules: Dict[str, nn.Module],
    weight_eq_obs_dict: Dict[str, _WeightEqualizationObserver],
) -> None:
    """ Converts the equalization operations and updates the other nodes in the
    following way:
        - Removes the input equalization observers and inserts a mul operator
          along with an equalization scale node wherever applicable (we do not
          want to insert a mul operator between connecting linear layers).
        - Updates the input quantization observers with the scaled input min/max
          values.
        - Scales the weights by the current and next equalization scales.
        - Removes the weight equalization observer node if it exists.

    Before (after prepare):
                                    weight values
                                          |
                                    WeightQuantObs
                                          |
                                      WeightEqObs
                                          |
        x -> InpQuantObs -> InpEqObs -> linear -> OutQuantObs

    After this function:
                                              scaled weight values
                                                      |
       equalization scale                       WeightQuantObs
              |                                       |
        x -> mul -> InpQuantObs (scaled min/max) -> linear -> OutQuantObs

    After convert:
       equalization scale                 scaled weight values
              |                                    |
        x -> mul -> quantize_per_tensor -> quantized::linear

    Note that although the equalization observer appeared after the quantization
    observer after prepare_fx, the mul node appears before the quantization node
    after convert_fx. This is because placing the equalization observer after
    the quantization observer in prepare_fx would allow us to keep the invariant
    that the graph before the current node inserts its observers is not
    modified.

    Having the equalization observer before the quantization observer would also
    cause some inconsistences between the ordering of the quantization and
    equalization observers.
    For example, a single linear layer would look like:
        x -> InpEqObs1 -> InpQuantObs1 -> linear1 -> OutQuantObs1
    But between two connected linear layers, it would look like:
        linear1 -> OutQuantObs1 -> InpEqObs2 -> linear2 -> OutQuantObs2
    """
    for node in model.graph.nodes:
        if node.op == 'call_module' and isinstance(modules[node.target], _InputEqualizationObserver):
            inp_quant_obs_node = node.args[0]
            prev_node = inp_quant_obs_node.args[0]

            # Update the following input quantization observer's min/max values
            scale_input_observer(node, modules)

            # If the previous node is a layer that needs to be equalized, then
            # we will remove the current node because we do not need to add any
            # equalization nodes between two layers that need to be equalized

            # Before: linear1 (prev_node) -> output_quant_obs1 (inp_quant_obs_node) -> input_eq_obs2 (node) -> linear2
            # After: linear1 (prev_node) -> output_quant_obs1 (inp_quant_obs_node) -> linear2
            if node_supports_equalization(prev_node, modules):
                remove_node(model, node, inp_quant_obs_node)
                continue

            # Remove the InputEqualization node and add a mul operator before
            # the quantization observer node that appears before the equalization node
            # Before: x -> input_quant_obs -> input_eq_obs -> linear
            # After: x -> mul -> input_quant_obs -> linear

            # Create a node containing the equalization scale
            with model.graph.inserting_before(inp_quant_obs_node):
                get_new_eq_scale_name = get_new_attr_name_with_prefix(prev_node.name + '_equalization_scale')
                name = get_new_eq_scale_name(modules)
                setattr(model, name, modules[node.target].equalization_scale)
                eq_scale_node = model.graph.create_node('get_attr', name)

            # Create a node multiplying the input with the equalization scale
            with model.graph.inserting_after(eq_scale_node):
                inputs = (prev_node, eq_scale_node)
                mul_node = model.graph.create_node("call_function", torch.mul, inputs)

            # Set the mul nod to be the input_quant_obs_node's input instead of
            # the previous node
            inp_quant_obs_node.replace_input_with(prev_node, mul_node)
            remove_node(model, node, inp_quant_obs_node)

        elif weight_eq_obs_dict.get(node.name, None) is not None:
            weight_eq_obs = weight_eq_obs_dict.get(node.name)
            assert(isinstance(weight_eq_obs, _WeightEqualizationObserver))
            equalization_scale = weight_eq_obs.equalization_scale
            maybe_next_equalization_scale = maybe_get_next_equalization_scale(node, modules)

            # Scale the weight nodes
            if node.op == 'call_module':
                scale_weight_node(node, modules, equalization_scale, maybe_next_equalization_scale)
            elif node.op == 'call_function':
                scale_weight_functional(node, model, modules, equalization_scale, maybe_next_equalization_scale)

                weight_eq_obs_node = maybe_get_functional_weight_node(node, modules, 'eq_obs')
                if weight_eq_obs_node is None:
                    return
                assert(isinstance(modules[str(weight_eq_obs_node.target)], _WeightEqualizationObserver))

                # Clear the quantization observer's min/max values so that they
                # can get updated later based on the new scale values
                clear_weight_quant_obs_node(node, modules)

                # Erase the weight equalization observer node
                prev_node = weight_eq_obs_node.args[0]
                remove_node(model, weight_eq_obs_node, prev_node)
            else:
                raise ValueError("Expected operation node to be 'call_module' or 'call_function" +
                                 f"Instead got node {node.name} as '{node.op}'.")

def _convert_equalization_ref(model: GraphModule):
    """ Reference function which applies changes needed for equalization, but
    does not quantize the nodes
    """
    modules = dict(model.named_modules(remove_duplicate=False))

    # Calculate the equalization scale, update the observers with the scaled
    # inputs, and scale the weight
    weight_eq_obs_dict = update_obs_for_equalization(model, modules)
    convert_eq_obs(model, modules, weight_eq_obs_dict)

    return GraphModule(model, model.graph)
