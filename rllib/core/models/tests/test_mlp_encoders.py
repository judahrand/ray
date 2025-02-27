import itertools
import unittest

from ray.rllib.core.models.configs import MLPEncoderConfig
from ray.rllib.core.models.base import ENCODER_OUT
from ray.rllib.utils.framework import try_import_tf, try_import_torch
from ray.rllib.utils.test_utils import framework_iterator, ModelChecker

_, tf, _ = try_import_tf()
torch, _ = try_import_torch()


class TestMLPEncoders(unittest.TestCase):
    def test_mlp_encoders(self):
        """Tests building MLP encoders properly and checks for correct architecture."""

        # Loop through different combinations of hyperparameters.
        inputs_dims_configs = [[1], [50]]
        list_of_hidden_layer_dims = [[], [1], [64, 64], [256, 256, 256]]
        hidden_layer_activations = [None, "linear", "relu", "tanh", "swish"]
        hidden_layer_use_layernorms = [False, True]
        output_dims_configs = inputs_dims_configs
        output_activations = hidden_layer_activations
        use_biases = [False, True]

        for permutation in itertools.product(
            inputs_dims_configs,
            list_of_hidden_layer_dims,
            hidden_layer_activations,
            hidden_layer_use_layernorms,
            output_activations,
            output_dims_configs,
            use_biases,
        ):
            (
                inputs_dims,
                hidden_layer_dims,
                hidden_layer_activation,
                hidden_layer_use_layernorm,
                output_activation,
                output_dims,
                use_bias,
            ) = permutation

            print(
                f"Testing ...\n"
                f"input_dims: {inputs_dims}\n"
                f"hidden_layer_dims: {hidden_layer_dims}\n"
                f"hidden_layer_activation: {hidden_layer_activation}\n"
                f"hidden_layer_use_layernorm: {hidden_layer_use_layernorm}\n"
                f"output_activation: {output_activation}\n"
                f"output_dims: {output_dims}\n"
                f"use_bias: {use_bias}\n"
            )

            config = MLPEncoderConfig(
                input_dims=inputs_dims,
                hidden_layer_dims=hidden_layer_dims,
                output_dims=output_dims,
                hidden_layer_activation=hidden_layer_activation,
                hidden_layer_use_layernorm=hidden_layer_use_layernorm,
                output_activation=output_activation,
                use_bias=use_bias,
            )

            # Use a ModelChecker to compare all added models (different frameworks)
            # with each other.
            model_checker = ModelChecker(config)

            for fw in framework_iterator(frameworks=("tf2", "torch")):
                # Add this framework version of the model to our checker.
                outputs = model_checker.add(framework=fw)
                self.assertEqual(outputs[ENCODER_OUT].shape, (1, output_dims[0]))

            # Check all added models against each other.
            model_checker.check()


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))
