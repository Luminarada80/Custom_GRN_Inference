import numpy as np
import pandas as pd
import dask.dataframe as dd
import logging
from typing import Union
from dask.dataframe.utils import is_dataframe_like

def minmax_normalize_dask(
    ddf: dd.DataFrame,
    score_cols: list[str],
    sample_frac: Union[None, float] = None,
    random_state: int = 42
) -> dd.DataFrame:
    """
    Min-max normalize selected columns. Optionally estimate min/max on a sample.
    """
    ddf = ddf.persist()

    if sample_frac:
        stats_df = ddf[score_cols].sample(frac=sample_frac, random_state=random_state).compute()
    else:
        stats_df = ddf[score_cols].compute()

    col_mins = stats_df.min().to_dict()
    col_maxs = stats_df.max().to_dict()

    def normalize_partition(df):
        if not is_dataframe_like(df):
            raise TypeError(f"Expected DataFrame, got {type(df)}")

        df = df.copy()
        for col in score_cols:
            min_val = col_mins[col]
            max_val = col_maxs[col]
            if max_val != min_val:
                df[col] = (df[col] - min_val) / (max_val - min_val)
            else:
                df[col] = 0.0
        return df

    return ddf.map_partitions(normalize_partition, meta=ddf._meta.copy())

def clip_and_normalize_log1p_pandas(
    df: pd.DataFrame,
    score_cols: list[str],
    quantiles: tuple[float, float] = (0.05, 0.95),
    apply_log1p: bool = True,
    sample_frac: Union[None, float] = None,
    random_state: int = 42
) -> pd.DataFrame:
    """
    Clips, normalizes, and optionally log1p-transforms selected columns in a pandas DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Input data.
    score_cols : list of str
        Column names to transform.
    quantiles : tuple
        Lower and upper quantiles (default 5th–95th).
    apply_log1p : bool
        Apply log1p after normalization.
    sample_frac : float or None
        If set, compute quantiles on a sample fraction to improve speed.
    random_state : int
        Random seed for reproducibility.

    Returns
    -------
    pd.DataFrame
        Transformed DataFrame.
    """
    df = df.copy()

    if sample_frac:
        logging.info(f"Sampling {sample_frac * 100:.1f}% of data to estimate quantiles")
        sample = df[score_cols].sample(frac=sample_frac, random_state=random_state)
    else:
        sample = df[score_cols]

    q_lo, q_hi = quantiles
    quantile_lows = sample.quantile(q_lo)
    quantile_highs = sample.quantile(q_hi)

    for col in score_cols:
        low, high = quantile_lows[col], quantile_highs[col]
        df[col] = df[col].clip(lower=low, upper=high)

        if high != low:
            df[col] = (df[col] - low) / (high - low)
        else:
            df[col] = 0.0

        if apply_log1p:
            df[col] = np.log1p(df[col])

    return df

def minmax_normalize_pandas(
    df: pd.DataFrame,
    score_cols: list[str],
) -> pd.DataFrame:
    """
    Applies global min-max normalization to selected columns in a Pandas DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        The Pandas DataFrame containing the columns to normalize.
    score_cols : list of str
        List of column names to normalize.

    Returns
    -------
    pd.DataFrame
        Normalized Pandas DataFrame.
    """
    df = df.copy()
    
    if isinstance(df, dd.DataFrame):
        df = df.compute()
        
    for col in score_cols:
        min_val = df[col].min()
        max_val = df[col].max()
        if max_val != min_val:
            df.loc[:, col] = (df[col] - min_val) / (max_val - min_val)
        else:
            df.loc[:, col] = 0.0
    return df
