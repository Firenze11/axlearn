# Copyright © 2024 Apple Inc.
"""Update Transformation Modules

Update transformations are typically used to implement optimizers like SGD, ADAM, etc.

In contrast to the legacy `optimizers.py`, these modules are implemented as actual AXlearn
`Module`s, that implement the `UpdateTransformation` interface.

An adapter class `WrappedPartitionedGradientTransformation` is provided to allow converting a
legacy `PartitionedGradientTransformation` to an `UpdateTransformation`.

Despite the `UpdateTransformation` interface being preferred for new optimizers, there
are no plans to stop supporting `PartitionedGradientTransformation`.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, Callable, Literal, Optional, Protocol, Union

import jax
import optax
from absl import logging
from jax.sharding import PartitionSpec

from axlearn.common import struct
from axlearn.common.base_layer import ParameterSpec
from axlearn.common.config import REQUIRED, ConfigOr, Required, config_class, maybe_instantiate
from axlearn.common.learner_base import LearnerModule
from axlearn.common.module import Module, OutputCollection
from axlearn.common.optimizer_base import OptParam, PartitionedGradientTransformation
from axlearn.common.utils import (
    Nested,
    Tensor,
    flatten_items,
    match_regex_rules,
    non_empty_leaf_merge_fn,
    tree_merge,
    tree_paths,
)


class UpdateTransformation(LearnerModule):
    """A Module to transform a model update.

    E.g., run an optimizer to transform raw gradients.

    For new optimizers, using this instead of `PartitionedGradientTransformation` is preferred
    because it supports more types of optimizers and allows better reuse of functionality across
    different optimizers.

    Despite this, there are no plans to stop supporting `PartitionedGradientTransformation`.
    """

    def transform_update(self, updates: Updates) -> Updates:
        """Compute the value and grad of `fun`."""
        raise NotImplementedError(type(self))

    def __call__(self, updates: Updates) -> Updates:
        """Alias for `transform_update()`."""
        return self.transform_update(updates)


class WrappedPartitionedGradientTransformation(UpdateTransformation):
    """An adapter allowing a `PartitionedGradientTransformation` to be used as an
    `UpdateTransformation`.
    """

    @config_class
    class Config(UpdateTransformation.Config):
        transformation: Required[ConfigOr[PartitionedGradientTransformation]] = REQUIRED

    def __init__(self, cfg: Config, *, parent: Optional[Module] = None):
        super().__init__(cfg, parent=parent)
        self.transformation: PartitionedGradientTransformation = maybe_instantiate(
            cfg.transformation
        )
        if not isinstance(self.transformation, PartitionedGradientTransformation):
            raise ValueError(
                f"Transformation must be a PartitionedGradientTransformation: {cfg.transformation}."
            )

    def create_state_partition_specs(
        self, model_param_specs: Nested[ParameterSpec]
    ) -> Union[Nested[PartitionSpec], tuple[Nested[PartitionSpec]],]:
        return self.transformation.partition(model_param_specs)

    def init(self, model_params: Nested[OptParam]) -> Nested[Tensor] | tuple[Nested[Tensor], ...]:
        return self.transformation.init(model_params)

    def transform_update(self, updates: Updates) -> Updates:
        """Run the `PartionedGradientTransformation.update` function to compute updates."""
        param_updates, optimizer_state = self.transformation.update(
            updates.delta_updates,
            state=self.state,
            params=updates.opt_params,
        )
        # Optimizer state from a PartitionedGradientTransformation may be a tuple, so we have to
        # assign it via the parent.
        self.get_invocation_context().set_state_update(optimizer_state)
        return dataclasses.replace(updates, delta_updates=param_updates)


class Updates(struct.PyTreeNode):
    """An update to model params and state that can be transformed."""

    # Updates needs to be a pytree for compatibility with `assertNestedAllClose()`

    # Params to be updated by the optimizer.
    # For backwards compatibility:
    # * The `Learner` implementation includes all
    #   parameters including those where `should_update_with_optimizers()` is `False`.
    # * The `CompositeLearner` implementation masks out entries that are assigned to another
    #   learner with `MaskedNode`.
    # Both None and optax.MaskedNode have been used for masking in various places in the
    # Learner codebase. For checkpoint compatibility, we allow both of them.
    opt_params: Nested[Union[OptParam, optax.MaskedNode, None]]

    # Additive updates to `opt_params`.
    # Can contain only a subset of leaf nodes in `opt_params`.
    delta_updates: Optional[Nested[Union[Tensor, optax.MaskedNode, None]]] = None

    # In-place updates to `opt_params`.
    # Can contain only a subset of leaf nodes in `opt_params`.
    # Takes precedence over `delta_updates`.
    inplace_updates: Optional[Nested[Union[Tensor, optax.MaskedNode, None]]] = None

    # The named forward passes that have previous been invoked.
    forward_pass: dict[str, ForwardPass] = struct.field(default_factory=dict)

    def param_values(self) -> Nested[Tensor]:
        """Returns a tree with the same structure as `opt_params` with the value of each param."""
        return jax.tree.map(
            lambda x: x.value, self.opt_params, is_leaf=lambda x: isinstance(x, OptParam)
        )

    def param_specs(self) -> Nested[ParameterSpec]:
        """Returns a tree with the same structure as `opt_params` with the metadata of each
        param.
        """
        return jax.tree.map(
            lambda x: ParameterSpec(
                shape=x.value.shape,
                dtype=x.value.dtype,
                factorization=x.factorization_spec,
                weight_decay_scale=x.weight_decay_scale,
            ),
            self.opt_params,
        )

    def mask(
        self,
        keep: Callable[[Nested], Nested[bool]],
        *,
        fields: Sequence[
            Literal["opt_params", "delta_updates", "inplace_updates", "forward_pass"]
        ] = ("opt_params", "delta_updates", "inplace_updates"),
    ) -> "Updates":
        """Return a copy of this instance where the values of the field have been
         masked using `optax.MaskedNode()` according to the leaves of `keep(self.field)` for
         each field in `fields`.

         Masking `forward_pass` is not implemented.

         Example:
             ```
             updates: Updates
             assert updates.delta_updates = dict(param=5)
             update = updates.mask(keep=lambda: False)
             assert updates.delta_updates = dict(param=optax.MaskedNode())
             ```

         Args:
             keep: A callable that will be called on each field to generate a tree of bools
                   The returned `tree` must be a prefix of the structure of the field it is
                   called on.
                   The leaves are replaced with `optax.MaskedNode()` where `keep(tree)` is False.
             fields: The fields to apply to.

         Returns:
             A masked version of this instance.

        Raises:
            NotImplementedError: If `fields` contains `forward_pass`.
        """
        replacements = {}
        for field in dataclasses.fields(self):
            if field.name in fields:
                value = getattr(self, field.name)
                replacements[field.name] = mask_tree(
                    value, keep=keep(value), mask_value=optax.MaskedNode()
                )
        return dataclasses.replace(self, **replacements)


class ForwardPass(struct.PyTreeNode):
    """The result of executing a `ForwardFn`."""

    # ForwardPass needs to be a pytree to prevent tracer leaks in `learner._value_and_grad()`.

    # The forward function.
    forward_fn: ForwardFn = struct.field(pytree_node=False)

    # Inputs to `forward_fn`.
    # The type is any pytree.
    inputs: Any
    # The model parameters used.
    model_params: Nested[Tensor]

    # The outputs from `forward_fn`.
    outputs: ForwardOutputs


class ForwardFn(Protocol):
    """Represents the model forward function."""

    def __call__(
        self,
        *,
        model_params: Nested[Tensor],
        inputs: Any,
    ) -> ForwardOutputs:
        """The forward function of a module.

        Args:
            model_params: The model params.
            inputs: The inputs for the forward function. Must be a pytree.

        Returns:
            A ForwardOutputs value.
        """


@dataclasses.dataclass
class ForwardBackwardOutputs:
    forward_outputs: ForwardOutputs
    backward_outputs: BackwardOutputs


class ForwardOutputs(struct.PyTreeNode):
    # ForwardOutputs needs to be a pytree to prevent tracer leaks in `learner._value_and_grad()`.
    loss: Tensor
    aux: Nested[Tensor]
    output_collection: OutputCollection


@dataclasses.dataclass
class BackwardOutputs:
    updated_params: Nested[Tensor]


def mask_tree(tree: dict, *, keep: dict, mask_value: Any) -> dict:
    """Mask out tree leaves that are not transformed by the optimizer.

    Args:
        tree: A nested structure with ParameterSpec, OptParams or Tensor as leaf nodes.
        keep: A tree of the same structure as tree, with boolean as leaf nodes. If
                the leaf is True, the original value of tree leaf is kept, otherwise replaced
                with `mask_value`.
        mask_value: The value to use to replace entries in `tree`.

    Returns:
        A masked tree the same structure as tree, the leaf is masked as MaskNode() if
            the corresponding keep leaf is False.

    """
    # For sub-learner optimizer state, only the subset of parameters
    # that belongs to the optimizer is kept and the rest is masked as optax.MaskNode().
    return jax.tree.map(
        lambda should_keep, leaf: leaf if should_keep else mask_value,
        keep,
        tree,
        is_leaf=lambda x: x is None,
    )


class OverrideInplaceUpdateTransformation(WrappedPartitionedGradientTransformation):
    """An update transformation that provides rules to override inplace updates.

    This update transformation moves gradients that match rules in `delta_updates` to
    `inplace_updates`, then applies `PartionedGradientTransformation.update`. Also, optimizer
    states won't be created for parameters that match these rules.
    """

    @config_class
    class Config(WrappedPartitionedGradientTransformation.Config):
        """Configures `OverrideInplaceUpdateTransformation`.

        Attributes:
            rules: list of regex rules to match.
        """

        rules: Required[Sequence[str]] = REQUIRED

    def _is_passthrough(self, params: Nested[Any]) -> Nested[bool]:
        """Gets a pytree of bools with True indicating a parameter or gradient is passthrough.

        Passthrough parameters are parameters that do not match the rules and follow the same
        semantic as a regular `WrappedPartitionedGradientTransformation`.
        """
        cfg: OverrideInplaceUpdateTransformation.Config = self.config
        rules = [(rule, False) for rule in cfg.rules]
        return jax.tree.map(
            lambda path: match_regex_rules(path, rules=rules, default_value=True),
            tree_paths(params),
        )

    def _keep_passthrough(self, params: Nested[Any]) -> Nested[Any]:
        """Given a pytree of params, keeps only the passthrough params."""
        return mask_tree(params, keep=self._is_passthrough(params), mask_value=optax.MaskedNode())

    def create_state_partition_specs(
        self, model_param_specs: Nested[ParameterSpec]
    ) -> Union[Nested[PartitionSpec], tuple[Nested[PartitionSpec]],]:
        return self.transformation.partition(self._keep_passthrough(model_param_specs))

    def init(self, model_params: Nested[OptParam]) -> Nested[Tensor] | tuple[Nested[Tensor], ...]:
        return self.transformation.init(self._keep_passthrough(model_params))

    def transform_update(self, updates: Updates) -> Updates:
        is_passthrough = self._is_passthrough(updates.delta_updates)
        override_inplace_updates = mask_tree(
            updates.delta_updates,
            keep=jax.tree.map(lambda x: not x, is_passthrough),
            mask_value=optax.MaskedNode(),
        )
        for path, value in flatten_items(override_inplace_updates):
            logging.info(
                "Applying inplace_updates instead of delta_updates for %s: %s%s.",
                path,
                str(value.dtype),
                str(value.shape),
            )

        passthrough_updates = super().transform_update(
            updates.mask(lambda _: is_passthrough, fields=["delta_updates", "opt_params"])
        )

        # Merge inplace updates back to `delta_updates` to make sure `delta_updates` has the same
        # tree structure as updates.opt_params, which is required by `Learner`. This won't affect
        # optimization result since `inplace_updates` will take priority.
        return dataclasses.replace(
            updates,
            delta_updates=tree_merge(
                passthrough_updates.delta_updates,
                secondary=override_inplace_updates,
                leaf_merge_fn=non_empty_leaf_merge_fn,
            ),
            inplace_updates=tree_merge(
                updates.inplace_updates,
                secondary=override_inplace_updates,
                leaf_merge_fn=non_empty_leaf_merge_fn,
            ),
        )
