import pandas as pd
import numpy as np
from pybiomart import Server
import matplotlib.pyplot as plt
from joblib import Parallel, delayed
from scipy import stats
from scipy.spatial import cKDTree
from statsmodels.stats.multitest import multipletests
from collections import defaultdict
from tqdm import tqdm
import ensembl
import normalization
import enhancerdb

organism = "hsapiens"
atac_data_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/K562/K562_human_filtered/K562_human_filtered_ATAC.csv"
rna_data_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/K562/K562_human_filtered/K562_human_filtered_RNA.csv"

enhancer_db_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/enhancer_db/enhancer"
peak_dist_limit = 1000000

def load_and_parse_atac_peaks(atac_data_file):
    atac_df = pd.read_csv(atac_data_file, sep=",", header=0, index_col=None)
    peak_pos = atac_df[atac_df.columns[0]].to_list()

    peak_df = pd.DataFrame()
    peak_df["peak_full"] = peak_pos
    peak_df["chr"] = [i.split(":")[0].strip("chr") for i in peak_pos]
    peak_df["start"] = [int(i.split(":")[1].split("-")[0]) for i in peak_pos]
    peak_df["end"] = [int(i.split(":")[1].split("-")[1]) for i in peak_pos]

    # Find the center of the peak (subtract 1/2 the length of the peak from the end)
    peak_df["center"] = peak_df["end"] - ((peak_df["end"] - peak_df["start"]) / 2)
    
    return atac_df, peak_df

def plot_column_histogram(df, colname, title):
    plt.figure(figsize=(5,5))
    plt.hist(df[colname], bins=30, edgecolor="k")
    plt.title(title)
    plt.xlabel(f"{colname} Score")
    plt.ylabel("Frequency")
    plt.tight_layout()
    plt.show()

def find_matching_peaks_kdtree(tss, chrom, tree_dict, threshold=10000):
    # Check if the chromosome is in our KD-Tree dictionary
    if chrom not in tree_dict:
        return []
    tree, idx_list = tree_dict[chrom]
    # If tss is a list or array, take the first value (or you could use np.mean)
    if isinstance(tss, (list, np.ndarray)):
        tss_val = float(tss[0])
    else:
        tss_val = float(tss)
    # Query the KDTree for all peaks within 'threshold' of the TSS
    indices = tree.query_ball_point(np.array([[tss_val]]), r=threshold)[0]
    # Convert tree indices back to the original peak_df indices
    return [idx_list[i] for i in indices]

def find_peaks_in_enhancer(enhancer, tree_dict):
    """
    Find peaks whose centers fall within an enhancer interval.
    
    Parameters:
      enhancer (Series): A row from the enhancer DataFrame with columns 'chr', 'start', 'end'
      tree_dict (dict): Dictionary mapping chromosome to (KDTree, index list)
      
    Returns:
      List of indices from the original peak_df that fall within the enhancer interval.
    """
    chrom = enhancer["chr"]
    start = enhancer["start"]
    end = enhancer["end"]
    
    # Check if the chromosome is present in our KDTree dictionary
    if chrom not in tree_dict:
        return []
    
    tree, idx_list = tree_dict[chrom]
    enhancer_center = (start + end) / 2.0
    enhancer_radius = (end - start) / 2.0
    
    # Query the KDTree for all peaks within the enhancer's interval
    indices = tree.query_ball_point(np.array([[enhancer_center]]), r=enhancer_radius)[0]
    return [idx_list[i] for i in indices]

def find_matching_peaks(peak_df, ensembl_gene_matching_chr, enhancer_db):
    # Build a dictionary mapping each chromosome to a KDTree and the corresponding index list
    tree_dict = {}
    for chrom, group in peak_df.groupby("chr"):
        # Reshape the center positions into a 2D array (required by cKDTree)
        centers = group["center"].values.reshape(-1, 1)
        tree = cKDTree(centers)
        tree_dict[chrom] = (tree, group.index.tolist())

    print(f"Identifying ATACseq peaks within {peak_dist_limit} of gene TSS using KDTree")
    # Apply the KDTree function row-wise
    ensembl_gene_matching_chr["peaks_in_range"] = ensembl_gene_matching_chr.apply(
        lambda row: find_matching_peaks_kdtree(row["Transcription start site (TSS)"],
                                            row["Chromosome/scaffold name"],
                                            tree_dict,
                                            threshold=peak_dist_limit),
        axis=1
    )

    # Filter out any genes that dont have any peaks within range
    ensembl_genes_within_range = ensembl_gene_matching_chr[ensembl_gene_matching_chr["peaks_in_range"].apply(lambda lst: len(lst) > 1)]

    # Find peaks within known enhancer regions
    enhancer_db["peaks_in_range"] = enhancer_db.apply(lambda row: find_peaks_in_enhancer(row, tree_dict), axis=1)

    # Remove enhancers with no peaks in range or that have a NaN score
    enhancer_db = enhancer_db[enhancer_db["peaks_in_range"].map(len) > 0].dropna()

    print("Num enhancers with mapped peaks by chromosome:")
    for chr, peaks in enhancer_db.groupby("chr"):
        print(f'\t{chr} = {peaks.shape[0]} mapped enhancers')
        
    return ensembl_genes_within_range, enhancer_db

def calculate_correlations(gene_row, rna_df_indexed, atac_df):
    """
    Calculate peak-to-gene and peak-to-peak correlations for a given gene row.
    
    Parameters:
      gene_row (Series): A row from ensembl_genes_within_range with:
          - "Gene name": gene symbol
          - "peaks_in_range": list of peak indices
      rna_df_indexed (DataFrame): RNA-seq data with index set to gene names.
      atac_df (DataFrame): ATAC-seq data (assumes first column is non-numeric).
    
    Returns:
      gene_peak_df (DataFrame): Each row is a peak with its correlation to the gene's expression.
      peak_peak_df (DataFrame): Each row is a unique peak pair with their correlation.
    """
    gene_name = gene_row["Gene name"]
    peak_indices = gene_row["peaks_in_range"]
    
    # Get gene expression vector for this gene
    try:
        gene_expr = rna_df_indexed.loc[gene_name].astype(float)
    except KeyError:
        return None, None

    # Extract ATAC-seq data for peaks (skip first column)
    selected_atac = atac_df.iloc[peak_indices, 1:].astype(float)
    # Filter out peaks with no accessibility
    selected_atac = selected_atac[selected_atac.sum(axis=1) > 0]
    if selected_atac.empty:
        return None, None
    
    # Compute peak-to-gene correlation (cells as rows)
    peak_to_gene_corr = selected_atac.transpose().corrwith(gene_expr).fillna(0)
    gene_peak_df = pd.DataFrame({
        "Gene": gene_name,
        "Peak": peak_to_gene_corr.index,
        "Correlation": peak_to_gene_corr.values
    })
    
    # Compute peak-to-peak correlations if there are at least 2 peaks
    if selected_atac.shape[0] > 1:
        # Use vectorized pandas function to compute the correlation matrix
        corr_matrix = selected_atac.transpose().corr().fillna(0).values
        peaks_idx = list(selected_atac.index)
        # Use numpy triu_indices to get the upper triangle (excluding diagonal)
        iu = np.triu_indices_from(corr_matrix, k=1)
        peak_pairs = [(peaks_idx[i], peaks_idx[j]) for i, j in zip(iu[0], iu[1])]
        corr_values = corr_matrix[iu]
        peak_peak_df = pd.DataFrame({
            "Peak1": [pair[0] for pair in peak_pairs],
            "Peak2": [pair[1] for pair in peak_pairs],
            "Correlation": corr_values
        })
    else:
        peak_peak_df = pd.DataFrame(columns=["Gene", "Peak1", "Peak2", "Correlation"])
    
    return gene_peak_df, peak_peak_df

def aggregate_all_correlations(genes_df, rna_df, atac_df, gene_range, n_jobs=8):
    """
    Aggregate peak-to-gene and peak-to-peak correlations for a range of genes using parallel processing.
    
    Parameters:
      genes_df (DataFrame): Gene information with peaks in range.
      rna_df (DataFrame): RNA-seq expression data (with "gene" column).
      atac_df (DataFrame): ATAC-seq data.
      gene_range (iterable): Row indices of genes_df to process.
      n_jobs (int): Number of parallel jobs.
      
    Returns:
      total_gene_peak_df (DataFrame): Aggregated gene-to-peak correlations.
      total_peak_peak_df (DataFrame): Aggregated peak-to-peak correlations.
    """
    rna_df_indexed = rna_df.set_index("gene")
    
    results = Parallel(n_jobs=n_jobs)(
        delayed(calculate_correlations)(genes_df.iloc[i], rna_df_indexed, atac_df)
        for i in tqdm(gene_range)
    )
    
    gene_peak_dfs = [res[0] for res in results if res[0] is not None]
    peak_peak_dfs = [res[1] for res in results if res[1] is not None and not res[1].empty]
    
    total_gene_peak_df = pd.concat(gene_peak_dfs, ignore_index=True) if gene_peak_dfs else pd.DataFrame()
    total_peak_peak_df = pd.concat(peak_peak_dfs, ignore_index=True) if peak_peak_dfs else pd.DataFrame()
    
    total_gene_peak_df = total_gene_peak_df.drop_duplicates()
    total_peak_peak_df = total_peak_peak_df.drop_duplicates()
    
    return total_gene_peak_df, total_peak_peak_df

def correlation_pvals_vec(r, n):
    """
    Vectorized function to compute two-sided p-values
    for an array of Pearson correlation coefficients r
    given sample size n.
    
    Parameters
    ----------
    r : np.ndarray
        Array of correlation coefficients.
    n : int
        Number of samples used to compute r.
        
    Returns
    -------
    pvals : np.ndarray
        Array of two-sided p-values for testing H0: r=0.
    """
    # Avoid division by zero by clamping r slightly away from ±1
    r_clamped = np.clip(r, -0.9999999999, 0.9999999999)
    
    # t-statistic: r * sqrt((n-2) / (1 - r^2))
    t_vals = r_clamped * np.sqrt((n - 2) / (1.0 - r_clamped**2))
    
    # Two-sided p-value from t-distribution with (n-2) df
    pvals = 2.0 * stats.t.sf(np.abs(t_vals), df=n - 2)
    return pvals

def compute_significance(df, n):
    """
    Given a DataFrame with columns 'Gene', 'Peak', 'Correlation',
    compute the p-values and FDR-corrected q-values.
    
    Returns the same DataFrame with additional columns 'pval' and 'qval'.
    """
    corrs = df["Correlation"].values
    # Compute p-values for each correlation
    pvals = correlation_pvals_vec(corrs, n)
    
    # FDR correction (Benjamini-Hochberg)
    _, qvals, _, _ = multipletests(pvals, alpha=0.05, method='fdr_bh')
    
    df["pval"] = pvals
    df["qval"] = qvals
    return df

def combine_peak_to_peak_with_peak_to_gene(peak_to_peak_df, peak_to_gene_df):

    print("\tRemoving peak to peak interactions where neither peak is assocated with a gene")
    # Only keep peaks in peak-to-peak if one of the peaks is associated with a gene
    # in peak-to-gene df
    peak_to_peak_df = peak_to_peak_df[
        peak_to_peak_df["peak1_full"].isin(peak_to_gene_df["peak_full"]) |
        peak_to_peak_df["peak2_full"].isin(peak_to_gene_df["peak_full"])
        ]
    
    # Assocate peak1 in peak-to-peak with a matching gene in peak-to-gene df
    print("\tAssigning peak1 with matching genes for peak2")
    merged_peak1 = pd.merge(
        peak_to_peak_df,
        peak_to_gene_df[['peak_full', 'Gene']],
        left_on="peak1_full",
        right_on="peak_full",
        how="inner"
    ).rename(columns={"Gene": "Gene_from_peak1"}).drop(columns=["peak_full"])

    # Assocate peak2 in peak-to-peak with a matching gene in peak-to-gene df
    print("\tAssigning peak2 with matching genes for peak1")
    merged_peak2 = pd.merge(
        peak_to_peak_df,
        peak_to_gene_df[['peak_full', 'Gene']],
        left_on="peak2_full",
        right_on="peak_full",
        how="inner"
    ).rename(columns={"Gene": "Gene_from_peak2"}).drop(columns=["peak_full"])

    # Combine the gene mappings for peak1 and peak2
    print('\tCombining gene mappings for peak1 and peak2')
    combined_long = pd.concat([merged_peak1, merged_peak2], ignore_index=True)

    # Merge the gene mappings for peak1 and peak2  into one column
    combined_long["Gene_list"] = combined_long[["Gene_from_peak1", "Gene_from_peak2"]].apply(
        lambda row: row.dropna().tolist(), axis=1
    )

    # Explode the list so that each gene gets its own row
    print("\tCreating list of peaks for each gene")
    combined_long = combined_long.explode("Gene_list").rename(columns={"Gene_list": "gene"})

    # Create a peak, score, gene dataframe for the peak1 column
    df1 = combined_long[["peak1_full", "Correlation", "Gene_from_peak1"]].rename(
        columns={"peak1_full": "peak", "Correlation": "score", "Gene_from_peak1": "gene"}
    )

    # Create a peak, score, gene dataframe for the peak2 column
    df2 = combined_long[["peak2_full", "Correlation", "Gene_from_peak2"]].rename(
        columns={"peak2_full": "peak", "Correlation": "score", "Gene_from_peak2": "gene"}
    )

    
    # Combine the two dataframes so that each peak can have multiple genes on different rows
    melted_df = pd.concat([df1, df2], ignore_index=True)

    # Makes sure to drop rows where Gene is NaN
    melted_df = melted_df.dropna(subset=["gene"])
    
    joined_df = melted_df[["peak", "gene"]].join(peak_to_gene_df[["peak_full", "Gene", "Correlation"]]).dropna(subset="Correlation")

    # Create a mapping from peak to correlation score from joined_df
    peak_to_corr = dict(zip(joined_df["peak"], joined_df["Correlation"]))

    # Map the 'peak' column in melted_df to get the new scores
    mapped_scores = melted_df["peak"].map(peak_to_corr)

    # Replace NaNs with the original 'score' from melted_df
    melted_df["score"] = mapped_scores.fillna(melted_df["score"])
    
    return melted_df

def normalize_peak_to_peak_scores(df):
    # Identify scores that are not 0 or 1
    mask = (df['score'] != 0) & (df['score'] != 1)
    filtered_scores = df.loc[mask, 'score']

    if not filtered_scores.empty:
        # Compute min and max of non-0/1 scores
        score_min = filtered_scores.min()
        score_max = filtered_scores.max()
        
        # Handle edge case where all non-0/1 scores are the same
        if score_max == score_min:
            # Set all non-0/1 scores to 0 (or another default value)
            score_normalized = np.where(mask, 0, df['score'])
        else:
            # Normalize non-0/1 scores and retain 0/1 values
            score_normalized = np.where(
                mask,
                (df['score'] - score_min) / (score_max - score_min),
                df['score']
            )
    else:
        # All scores are 0 or 1; no normalization needed
        score_normalized = df['score']
    
    return score_normalized

# def main():
print("Loading the scRNA-seq dataset")
rna_df: pd.DataFrame = pd.read_csv(rna_data_file, sep=",", header=0, index_col=None)
rna_df = rna_df.rename(columns={rna_df.columns[0]: "gene"})

print("Loading and parsing ATAC peak positions")
atac_df, peak_df = load_and_parse_atac_peaks(atac_data_file)

print("Log2 counts per million Normalizing the ATAC-seq dataset")
atac_df: pd.DataFrame = normalization.log2_cpm_normalize(atac_df)

print("Loading enhancer database")    
enhancer_db: pd.DataFrame = enhancerdb.load_enhancer_database_file(enhancer_db_file)

print(f"Loading ensembl genes for {organism}")
ensembl_gene_df: pd.DataFrame = ensembl.retrieve_ensembl_gene_positions(organism)

print(f"Matching ensembl genes and chromosomes to those in the data")
# Subset to only contain genes that are in the RNA dataset
ensembl_gene_matching_genes = ensembl_gene_df[ensembl_gene_df["Gene name"].isin(rna_df["gene"])].dropna()

# Subset to only contain chromosomes and scaffolds that are present in the peak dataframe "chr" column
ensembl_gene_matching_chr = ensembl_gene_matching_genes[ensembl_gene_matching_genes["Chromosome/scaffold name"].isin(peak_df["chr"])].dropna()

ensembl_genes_within_range, enhancer_db = find_matching_peaks(peak_df, ensembl_gene_matching_chr, enhancer_db)

# Isolate only the enhancer_db scores for the peaks that mapped to enhancers
enhancer_db_peak_scores = enhancer_db[["score", "peaks_in_range"]]

# Create a dictionary to collect scores for each peak index.
peak_scores = defaultdict(list)

# Iterate through each row and append all tissue scores to a list for each peak
for _, row in enhancer_db_peak_scores.iterrows():
    score = row['score']
    for peak in row['peaks_in_range']:
        peak_scores[peak].append(score)

# Compute the average score for each peak (Peaks have multiple scores for diff tissues)
peak_avg = {peak: sum(scores) / len(scores) for peak, scores in peak_scores.items()}

# Convert the dictionary to a DataFrame
enhancer_db_peak_scores = pd.DataFrame(list(peak_avg.items()), columns=['peak_index', 'score'])

# Optionally, sort the DataFrame by peak_index
enhancer_db_peak_scores = enhancer_db_peak_scores.sort_values('peak_index').reset_index(drop=True)
    

print("Calculating peak-to-peak and peak-to-gene correlations")
gene_indices = range(1, 5000) 
total_gene_peak_df, total_peak_peak_df = aggregate_all_correlations(
    ensembl_genes_within_range, rna_df, atac_df, gene_indices, n_jobs=8
)

print("Aggregated Gene-to-Peak Correlations:")
print(total_gene_peak_df.head())

print("\nAggregated Peak-to-Peak Correlations:")
print(total_peak_peak_df.head())

print("Calculating significance of peak to gene correlations")
total_gene_peak_df = compute_significance(total_gene_peak_df, total_gene_peak_df.shape[0])

print("\tFiltering for significant peaks")
sig_gene_peak_df = total_gene_peak_df[total_gene_peak_df["pval"] < 0.05].copy()
sig_gene_peak_df["log2_pval"] = np.log2(total_gene_peak_df["pval"] + 1e-10)

print("Calculating significance of peak to peak correlations")
total_peak_peak_df = compute_significance(total_peak_peak_df, total_peak_peak_df.shape[0])

print("\tFiltering for significant peaks")
sig_peak_peak_df = total_peak_peak_df[total_peak_peak_df["pval"] < 0.05].copy()
sig_peak_peak_df["log2_pval"] = np.log2(sig_peak_peak_df["pval"] + 1e-10)

# plot_column_histogram(total_peak_peak_df, "Correlation", "Peak-to-Peak Correlation Scores")
# plot_column_histogram(total_gene_peak_df, "Correlation", "Peak-to-Gene Correlation Scores")
# plot_column_histogram(sig_gene_peak_df, "log2_pval", "Peak to Gene Score p-values")
# plot_column_histogram(sig_peak_peak_df, "log2_pval", "Peak to Peak Score p-values")

# Map the peak indices to their full location strings
sig_gene_peak_df["peak_full"] = sig_gene_peak_df["Peak"].map(peak_df["peak_full"])
sig_peak_peak_df["peak1_full"] = sig_peak_peak_df["Peak1"].map(peak_df["peak_full"])
sig_peak_peak_df["peak2_full"] = sig_peak_peak_df["Peak2"].map(peak_df["peak_full"])

# Map the enhancer scores by peak indices (Dont add a score for enhancers with no prior scores)
sig_gene_peak_df["enhancer_score"] = sig_gene_peak_df["Peak"].map(enhancer_db_peak_scores["score"]).fillna(0)
sig_peak_peak_df["peak1_enhancer_score"] = sig_peak_peak_df["Peak1"].map(enhancer_db_peak_scores["score"]).fillna(0)
sig_peak_peak_df["peak2_enhancer_score"] = sig_peak_peak_df["Peak2"].map(enhancer_db_peak_scores["score"]).fillna(0)

print("Significant peak to gene correlations")
print(sig_gene_peak_df.head())
print()
print("Significant peak to peak correlations")
print(sig_peak_peak_df.head())

peak_gene_format_df = sig_gene_peak_df[["peak_full", "Gene", "Correlation", "enhancer_score"]]
peak_peak_format_df = sig_peak_peak_df[["peak1_full", "peak2_full", "Correlation", "peak1_enhancer_score", "peak2_enhancer_score"]]

# Associate significant peak-to-peak scores with the peak-to-gene scores.
# If a peak regulates a peak that regulates a gene, set the peak's score to the gene score instead 
# of the peak-to-peak score
print("Combining peak to peak with peak to gene scores")
melted_df = combine_peak_to_peak_with_peak_to_gene(peak_peak_format_df, peak_gene_format_df)

print("Normalizing peak to peak scores between 0-1")
melted_df["score"] = normalize_peak_to_peak_scores(melted_df)

print(melted_df.head())

# melted_df.to_csv("/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/output/K562/K562_human_filtered/peak_to_tg_scores.csv", sep="\t", header=True, index=False)

# if __name__ == "__main__":
#     main()