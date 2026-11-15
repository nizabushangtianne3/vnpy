"""
LightGBM model for alpha factor prediction.
"""

from typing import cast

import numpy as np
import pandas as pd
import polars as pl
import lightgbm as lgb
import matplotlib.pyplot as plt

from vnpy.alpha.dataset import AlphaDataset, Segment
from vnpy.alpha.model import AlphaModel


class LgbModel(AlphaModel):
    """LightGBM ensemble learning algorithm"""

    def __init__(
        self,
        learning_rate: float = 0.1,
        num_leaves: int = 31,
        num_boost_round: int = 1000,
        early_stopping_rounds: int = 50,
        log_evaluation_period: int = 1,
        seed: int | None = None,
        min_data_in_leaf: int = 20,
        feature_fraction: float = 1.0,
        bagging_fraction: float = 1.0,
        bagging_freq: int = 0,
        lambda_l1: float = 0.0,
        lambda_l2: float = 0.0
    ) -> None:
        """
        Initialize an MSE LightGBM booster with early stopping and periodic training logs.

        The regularisation arguments are exposed because a model fitted on a
        market-sized cross-section has 2700000 rows to learn 158 factors from,
        and the unrestricted default will spend a thousand boosting rounds
        memorising the training window instead of learning the relationship
        that holds on the next one. What the gate reads is the stability of
        the signal outside the window, so how much the booster is allowed to
        fit is part of the model rather than a detail of the training loop.
        """
        self.params: dict = {
            "objective": "mse",
            "learning_rate": learning_rate,
            "num_leaves": num_leaves,
            "seed": seed,
            "min_data_in_leaf": min_data_in_leaf,
            "feature_fraction": feature_fraction,
            "bagging_fraction": bagging_fraction,
            "bagging_freq": bagging_freq,
            "lambda_l1": lambda_l1,
            "lambda_l2": lambda_l2
        }

        self.num_boost_round: int = num_boost_round
        self.early_stopping_rounds: int = early_stopping_rounds
        self.log_evaluation_period: int = log_evaluation_period

        self.model: lgb.Booster | None = None

    def _prepare_data(self, dataset: AlphaDataset) -> list[lgb.Dataset]:
        """
        Build LightGBM datasets for the train segment and then the validation segment.
        """
        ds: list[lgb.Dataset] = []

        # Process training and validation separately
        segment: Segment
        for segment in [Segment.TRAIN, Segment.VALID]:
            # Get data for learning
            df: pl.DataFrame = dataset.fetch_learn(segment)
            df = df.sort(["datetime", "vt_symbol"])

            # Convert to numpy arrays
            data: pd.DataFrame = df.select(df.columns[2: -1]).to_pandas()
            label: np.ndarray = np.array(df["label"])

            # Add training data
            ds.append(lgb.Dataset(data, label=label))

        return ds

    def fit(self, dataset: AlphaDataset) -> None:
        """
        Train the booster on the train and validation sets, stopping early when validation stalls.
        """
        # Prepare task data
        ds: list[lgb.Dataset] = self._prepare_data(dataset)

        # Execute model training
        self.model = lgb.train(
            self.params,
            ds[0],
            num_boost_round=self.num_boost_round,
            valid_sets=ds,
            valid_names=["train", "valid"],
            callbacks=[
                lgb.early_stopping(self.early_stopping_rounds),      # Early stopping callback
                lgb.log_evaluation(self.log_evaluation_period)       # Logging callback
            ]
        )

    def predict(self, dataset: AlphaDataset, segment: Segment) -> np.ndarray:
        """
        Predict one data segment, or raise ValueError if the booster has not been fitted.
        """
        # Check if model exists
        if self.model is None:
            raise ValueError("model is not fitted yet!")

        # Get data for inference
        df: pl.DataFrame = dataset.fetch_infer(segment)
        df = df.sort(["datetime", "vt_symbol"])

        # Convert to numpy array
        data: np.ndarray = df.select(df.columns[2: -1]).to_numpy()

        # Return prediction results
        result: np.ndarray = cast(np.ndarray, self.model.predict(data))
        return result

    def detail(self) -> None:
        """
        Plot up to 50 features by split and by gain, and return immediately if the model is not fitted.
        """
        if not self.model:
            return

        importance_type: str
        for importance_type in ["split", "gain"]:
            ax: plt.Axes = lgb.plot_importance(
                self.model,
                max_num_features=50,
                importance_type=importance_type,
                figsize=(10, 20)
            )
            ax.set_title(f"Feature Importance ({importance_type})")
