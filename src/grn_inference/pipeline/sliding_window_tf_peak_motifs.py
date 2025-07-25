#!/usr/bin/env python3
import argparse
import logging
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Union

import dask.dataframe as dd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from Bio import SeqIO
from matplotlib.ticker import FuncFormatter

from numba import njit, prange
from pyarrow.lib import ArrowInvalid
from pyarrow.parquet import ParquetFile
from scipy import stats
from tqdm import tqdm

# from grn_inference.normalization import minmax_normalize_dask
from grn_inference.plotting import plot_feature_score_histogram
from grn_inference.normalization import (
    clip_and_normalize_log1p_pandas,
    minmax_normalize_pandas
)
from grn_inference.create_homer_peak_file import format_peaks

# at module top‐level
_global_chr_pos_to_seq: Union[None, pd.DataFrame] = None
_global_tf_df: Union[None, pd.DataFrame] = None
_global_bg_freq: Union[None, pd.Series] = None
_global_plus: Union[None, np.ndarray] = None
_global_minus: Union[None, np.ndarray] = None

def _init_worker(chr_pos_to_seq, tf_df, bg_freq, shared_plus, shared_minus):
    global _global_chr_pos_to_seq, _global_tf_df, _global_bg_freq, _global_plus, _global_minus
    _global_chr_pos_to_seq = chr_pos_to_seq
    _global_tf_df          = tf_df
    _global_bg_freq        = bg_freq
    _global_plus           = shared_plus
    _global_minus          = shared_minus

def parse_args() -> argparse.Namespace:
    """
    Parses command-line arguments.

    Returns:
    argparse.Namespace: Parsed arguments containing paths for input and output files.
    """
    parser = argparse.ArgumentParser(description="Process TF motif binding potential.")
    parser.add_argument(
        "--tf_names_file",
        type=str,
        required=True,
        help="Path to the tab-separated TF_Information_all_motifs.txt file containing TF name to binding motif association"
    )
    parser.add_argument(
        "--meme_dir",
        type=str,
        required=True,
        help="Path to the directory containing the motif .meme files for the organism"
    )
    parser.add_argument(
        "--reference_genome_dir",
        type=str,
        required=True,
        help="Path to the directory containing the chromosome fasta files for an organism"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        required=True,
        help="Path to the output directory for the sample"
    )
    parser.add_argument(
        "--species",
        type=str,
        required=True,
        help="Species of the sample, either 'mouse', 'human', 'hg38', or 'mm10'"
    )
    parser.add_argument(
        "--num_cpu",
        type=str,
        required=True,
        help="Number of processors to run multithreading with"
    )
    parser.add_argument(
        "--fig_dir",
        type=str,
        required=True,
        help="Output directory for figueres"
    )
    
    args: argparse.Namespace = parser.parse_args()

    return args

@njit(parallel=True)
def score_all_peaks(seqs_plus, seqs_minus, pwm_values):
    n_peaks, L = seqs_plus.shape
    wsize = pwm_values.shape[0]
    out = np.zeros(n_peaks, dtype=np.float64)

    for i in prange(n_peaks):
        total = 0.0
        for strand in (seqs_plus[i], seqs_minus[i]):
            # slide the window
            for j in range(L - wsize + 1):
                s = 0.0
                for k in range(wsize):
                    idx = strand[j + k]
                    # skip ambiguous bases (idx < 0 or >=4)
                    if 0 <= idx < 4:
                        s += pwm_values[k, idx]
                total += s
        out[i] = total
    return out

def get_background_freq(species):
    if species == "human" or species == "hg38" or species == "mmusculus":
        background_freq = pd.Series({
            "A": 0.29182,
            "C": 0.20818,
            "G": 0.20818,
            "T": 0.29182
        })
    
    elif species == "mouse" or species == "mm10" or species == "hsapiens":
        background_freq = pd.Series({
        "A": 0.2917,
        "C": 0.2083,
        "G": 0.2083,
        "T": 0.2917
    })
        
    else:
        raise Exception(f"Species {species} is not 'human', 'mouse', 'hg38', or 'mm10'")

    return background_freq
    

def process_motif_file_and_save(file, meme_dir, output_dir):
    try:
        # Load the PWM
        assert _global_bg_freq is not None, "Background frequencies must be set before calling PWM scoring."
        bg: pd.Series[float] = _global_bg_freq
        motif_df = pd.read_csv(os.path.join(meme_dir, file), sep="\t", header=0, index_col=0)
        pwm = np.log2(motif_df.T.div(bg, axis=0).add(1)).T
        pwm = np.vstack([pwm, np.zeros((1, 4))])  # add row for 'N'

        # Score every peak on both strands in parallel
        scores = score_all_peaks(_global_plus, _global_minus, pwm)

        # Look up TFs for this motif
        motif_name = file.replace('.txt', '')
        
        assert _global_tf_df is not None, "Global TF‐to‐motif DataFrame was never initialized"
        assert _global_chr_pos_to_seq is not None, "Global peak position to sequence DataFrame was never initialized"
        
        mask = _global_tf_df["Motif_ID"] == motif_name
        tf_names = _global_tf_df.loc[mask]["TF_Name"].values

        peak_ids = _global_chr_pos_to_seq.apply(
            lambda row: f'{row["chromosome"]}:{row["start"]}-{row["end"]}', axis=1
        )

        tmp_dir = os.path.join(output_dir, "tmp", "sliding_window_tf_scores")
        os.makedirs(tmp_dir, exist_ok=True)

        for tf in tf_names:
            df = pd.DataFrame({
                "peak_id": peak_ids,
                "source_id": tf,
                "sliding_window_score": scores
            })
            df = df.dropna()

            tf_out_path = os.path.join(tmp_dir, f"{tf}.parquet")
            table = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table, tf_out_path, compression="snappy")

        return True

    except Exception as e:
        logging.error(f"Error processing {file}: {e}")
        return False

def is_valid_parquet(file_path):
    try:
        ParquetFile(file_path)
        return True
    except (ArrowInvalid, OSError):
        return False

def get_valid_parquet_files(directory: str) -> list[str]:
    valid_files = []
    for f in os.listdir(directory):
        if f.endswith(".parquet"):
            full_path = os.path.join(directory, f)
            try:
                _ = pq.ParquetFile(full_path).metadata  # trigger metadata read
                valid_files.append(full_path)
            except Exception as e:
                logging.warning(f"Skipping corrupt Parquet file: {f}\n  Reason: {e}")
    return valid_files

def associate_tf_with_motif_pwm(tf_names_file, meme_dir, chr_pos_to_seq, gene_names, species, num_cpu, output_dir):
    logging.info("Preparing for parallel motif scoring...")

    # Background nucleotide frequencies
    background_freq = get_background_freq(species)

    # Load TF-to-motif mapping
    tf_df = pd.read_csv(tf_names_file, sep="\t", header=0)
    tf_df = tf_df[tf_df["TF_Name"].isin(gene_names)]
    logging.info(f"Number of TFs matching RNA dataset = {tf_df.shape[0]}")

    tf_motif_names = tf_df["Motif_ID"].unique().tolist()
    logging.info(f"Number of motifs: {len(tf_motif_names)}")
    logging.info(f"Number of peaks: {chr_pos_to_seq.shape[0]}")
    
    # Determine max length of all sequences
    chr_pos_to_seq["peak_len"] = chr_pos_to_seq["+ seq"].apply(len)
    max_len = chr_pos_to_seq["peak_len"].max()
    logging.info(f"\tMaximum peak length: {max_len} bp")

    # Pad all sequences to max_len
    def pad_sequences(seqs, fixed_len, pad_val=-1):
        padded = np.full((len(seqs), fixed_len), pad_val, dtype=np.int8)
        for i, seq in enumerate(seqs):
            padded[i, :len(seq)] = seq[:fixed_len]
        return padded

    seqs_plus = pad_sequences(chr_pos_to_seq["+ seq"].to_list(), fixed_len=max_len)
    seqs_minus = pad_sequences(chr_pos_to_seq["- seq"].to_list(), fixed_len=max_len)

    # Directory for cached output
    tmp_dir = os.path.join(output_dir, "tmp", "sliding_window_tf_scores")
    os.makedirs(tmp_dir, exist_ok=True)

    # Filter motif files for motifs where NOT all associated TF files are cached
    logging.info(f"Checking the tmp directory for existing sliding window results for each TF...")
    filtered_motif_files = []
    for motif_file in os.listdir(meme_dir):
        motif_id = motif_file.replace('.txt', '')
        if motif_id in tf_motif_names:
            tf_names = tf_df[tf_df["Motif_ID"] == motif_id]["TF_Name"].values
            all_cached = all(
                is_valid_parquet(os.path.join(tmp_dir, f"{tf}.parquet"))
                for tf in tf_names
            )
            if not all_cached:
                filtered_motif_files.append(motif_file)

    if len(filtered_motif_files) > 0:
        logging.info(f"\t- Number of motif files found: {len(filtered_motif_files)} / {len(tf_motif_names)}")

        logging.info(f"\nCalculating sliding window motif scores for each ATAC-seq peak")
        logging.info(f"\tUsing {num_cpu} processors")
        logging.info(f"\tSize of calculation: {len(filtered_motif_files)} motifs × {chr_pos_to_seq.shape[0]} peaks")

        with ProcessPoolExecutor(
            max_workers=num_cpu,
            initializer=_init_worker,
            initargs=(chr_pos_to_seq, tf_df, background_freq, seqs_plus, seqs_minus)
        ) as executor:
            futures = {
                executor.submit(process_motif_file_and_save, f, meme_dir, output_dir): f
                for f in filtered_motif_files
            }

            min_update = max(1, int(0.02 * len(futures)))
            for future in tqdm(as_completed(futures), total=len(futures), desc="Scoring motifs", miniters=min_update):
                _ = future.result()
        logging.info("Finished scoring all motifs. Reading TF motif parquet files...")
    else:
        logging.info("\nAll TFs have pre-existing parquet files in the tmp directory, reading cached parquet files...")
    
    parquet_dir = os.path.join(output_dir, "tmp", "sliding_window_tf_scores")
    valid_parquet_files = get_valid_parquet_files(parquet_dir)

    if not valid_parquet_files:
        raise RuntimeError("No valid TF motif parquet files found after filtering.")

    ddf = dd.read_parquet(valid_parquet_files)
    df = ddf.compute()
    
    # normalized_df = clip_and_normalize_log1p_pandas(
    #     df=df,
    #     score_cols=["sliding_window_score"],
    #     quantiles=(0.05, 0.95),
    #     apply_log1p=True,
    # )
    
    # normalized_df = minmax_normalize_pandas(
    #     df=normalized_df, 
    #     score_cols=["sliding_window_score"], 
    # )
    
    return df

def find_ATAC_peak_sequence(peak_df, reference_genome_dir, parsed_peak_file, fig_dir):
    logging.info("Reading in ATACseq peak file")
    # Read in the Homer peaks dataframe
    chr_seq_list = []
    logging.info("Finding DNA sequence for each ATAC peak")
    
    logging.info("Reading in genome fasta files")
    files_to_open = []
    for file in os.listdir(reference_genome_dir):
        if ".fa" in file:
            
            file_path = os.path.join(reference_genome_dir, file)
            files_to_open.append(file_path)
    
    lookup = np.full(256, -1, dtype=np.int8)  # Default: ambiguous characters get -1.
    lookup[ord('A')] = 0
    lookup[ord('C')] = 1
    lookup[ord('G')] = 2
    lookup[ord('T')] = 3
    lookup[ord('N')] = 4
    
    logging.info(f'Extracting the peak sequences...')
    # Find the unique chromosomes in the peaks
    peak_chr_ids = set(peak_df["chromosome"].unique())
    
    # Iterate through each fasta file (chromosome fastas for mouse, entire genome fasta for human)
    for file in tqdm(files_to_open):
        
        # Read in the fasta
        fasta_sequences = SeqIO.parse(open(file), 'fasta')
        
        # Find the sequence for each peak in the ATACseq data
        for fasta_rec in fasta_sequences:
            if fasta_rec.id in peak_chr_ids:
                chr_seq_plus = str(fasta_rec.seq).upper()
                chr_seq_neg = str(fasta_rec.seq.complement()).upper()
                
                chr_peaks = peak_df.loc[
                    peak_df["chromosome"] == fasta_rec.id,
                    ["peak_id", "chromosome", "start", "end"]
                ].copy()
                
                starts = chr_peaks["start"].to_numpy()
                ends = chr_peaks["end"].to_numpy()
                
                # Convert the sequence string into a NumPy array of uint8 codes, then map:
                chr_seq_plus_mapped = lookup[np.frombuffer(chr_seq_plus.encode('ascii'), dtype=np.uint8)]
                chr_seq_neg_mapped  = lookup[np.frombuffer(chr_seq_neg.encode('ascii'), dtype=np.uint8)]
                
                chr_peaks["+ seq"] = [chr_seq_plus_mapped[start:end] for start, end in zip(starts, ends)]
                chr_peaks["- seq"] = [chr_seq_neg_mapped[start:end] for start, end in zip(starts, ends)]
                
                chr_peaks = chr_peaks.dropna()
                chr_seq_list.append(chr_peaks)

    # Mouse has separate fasta for each chromosome
    if len(chr_seq_list) > 1:
        chr_pos_to_seq = pd.concat(chr_seq_list)
    
    # Human fasta is one file with all chromosomes
    else:
        chr_pos_to_seq = chr_seq_list[0]
        
    peak_lengths = peak_df["end"] - peak_df["start"]
    mode_length = int(stats.mode(peak_lengths, keepdims=False).mode)
    logging.info(f"\tMost common peak length (mode): {mode_length} bp")
    
    logging.info(f'\t    - Saving histogram of peak lengths')
    plt.figure(figsize=(8,6))
    plt.hist(peak_lengths, bins=50, edgecolor='black')
    plt.xlabel("Peak length (bp)", fontsize=14)
    plt.ylabel("Count", fontsize=14)
    plt.title("Distribution of ATAC peak lengths", fontsize=16)
    plt.grid(False)
    plt.savefig(os.path.join(fig_dir, "atac_peak_len_hist.png"), dpi=200)
        
    logging.info(f'\tFound sequence for {chr_pos_to_seq.shape[0] / peak_df.shape[0] * 100}% of peaks ({chr_pos_to_seq.shape[0]} / {peak_df.shape[0]})')
    
    return chr_pos_to_seq

def replace_nth(sub,repl,txt,nth):
    arr=txt.split(sub)
    part1=sub.join(arr[:nth])
    part2=sub.join(arr[nth:])

    return part1+repl+part2

def main():
    # Parse arguments
    args: argparse.Namespace = parse_args()
    tf_names_file: str = args.tf_names_file
    meme_dir: str = args.meme_dir
    reference_genome_dir: str = args.reference_genome_dir
    output_dir: str = args.output_dir
    species: str = args.species
    num_cpu: int = int(args.num_cpu)
    fig_dir: str = args.fig_dir
    
    tmp_dir = f"{output_dir}/tmp"
    os.makedirs(tmp_dir, exist_ok=True)
    
    # Alternative: Set file names manually
    # tf_names_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/motif_information/mm10/TF_Information_all_motifs.txt"
    # meme_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/motif_information/mm10/mm10_motif_meme_files"
    # reference_genome_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/reference_genome/mm10"
    # atac_data_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/mESC_filtered_L2_E7.5_merged_ATAC.csv"
    # rna_data_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/mESC_filtered_L2_E7.5_merged_RNA.csv"
    # output_dir = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/output/mESC"
    # num_cpu = 4
    
    # Use the peaks and gene names only for peaks that are within 1 MB of the gene's TSS (from preprocessing)
    logging.info(f"Reading Peaks and Genes from 'tss_distance_score.parquet'")
    assert os.path.isfile(os.path.join(output_dir, "tss_distance_score.parquet")), FileNotFoundError("tss_distance_score.parquet not found in output_dir")
    
    # Runs using peak-gene pairs from the ATAC-seq and RNA-seq datasets
    # atac_df = pd.read_parquet("/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/DS011_mESC/DS011_mESC_sample1/DS011_mESC_ATAC_processed.parquet")
    # rna_df = pd.read_parquet("/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/DS011_mESC/DS011_mESC_sample1/DS011_mESC_RNA_processed.parquet")
    # peak_ids = atac_df["peak_id"]
    # gene_names = set(rna_df["gene_id"].dropna())
    
    # Runs using the pre-processed peaks within 1 Mb of a gene's TSS
    peaks_near_genes_df: pd.DataFrame = pd.read_parquet(os.path.join(output_dir, "tss_distance_score.parquet"))
    peak_ids = peaks_near_genes_df["peak_id"].drop_duplicates()
    gene_names = peaks_near_genes_df["target_id"].drop_duplicates()
    
    # Read in the peak dataframe containing genomic sequences    
    parsed_peak_file = f'{tmp_dir}/peak_sequences.pkl'
    if os.path.isfile(parsed_peak_file):
        logging.info('Reading ATACseq peaks from pickle file')
        chr_pos_to_seq = pd.read_pickle(parsed_peak_file)
        
    # Create the peak dataframe containing genomic sequences if it doesn't exist
    else:
            
        logging.info(f'  - Identifying ATACseq peak sequences')
        peak_df = format_peaks(peak_ids)
        
        # Get the genomic sequence from the reference genome to each ATACseq peak
        chr_pos_to_seq = find_ATAC_peak_sequence(peak_df, reference_genome_dir, parsed_peak_file, fig_dir)
        
        # Write the peak sequences to a pickle file in the tmp dir
        logging.info('Writing to pickle file')
        chr_pos_to_seq.to_pickle(parsed_peak_file)
        logging.info(f'\tDone!')
        
    # Associate the TFs from TF_Information_all_motifs.txt to the motif with the matching motifID
    df = associate_tf_with_motif_pwm(
        tf_names_file, meme_dir, chr_pos_to_seq,
        gene_names, species, num_cpu, output_dir
    )
    
    plot_feature_score_histogram(df, "sliding_window_score", output_dir)

    df.to_parquet(f"{output_dir}/sliding_window_tf_to_peak_score.parquet", engine="pyarrow", compression="snappy")
    logging.info(f"Wrote final TF–peak sliding window scores to sliding_window_tf_to_peak_score.parquet")

    
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    main()
