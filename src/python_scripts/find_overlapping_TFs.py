import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

RNA_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/input/macrophage_buffer1_filtered_RNA.csv"
TF_motif_binding_file = "/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/output/total_motif_regulatory_scores.tsv"

print(f'Loading datasets')
RNA_dataset = pd.read_csv(RNA_file)
TF_motif_binding_df = pd.read_csv(TF_motif_binding_file, header=0, sep="\t", index_col=None)

# Find overlapping TFs
RNA_dataset = RNA_dataset.rename(columns={RNA_dataset.columns[0]: "Genes"})

genes = set(RNA_dataset["Genes"])

overlapping_TF_motif_binding_df = TF_motif_binding_df[
    (TF_motif_binding_df["Source"].apply(lambda x: x in genes)) &
    (TF_motif_binding_df["Target"].apply(lambda x: x in genes))
    ]

# Align RNA_dataset with overlapping_TF_motif_binding_df["Source"]
aligned_RNA = RNA_dataset[RNA_dataset["Genes"].isin(overlapping_TF_motif_binding_df["Source"])]

RNA_expression_matrix = RNA_dataset.iloc[1:, 1:].values

threshold = np.mean(RNA_expression_matrix) * np.std(RNA_expression_matrix)
print(f'Threshold = {threshold}')

row_sums = RNA_dataset.iloc[1:, 1:].sum(axis=1)  # Compute row sums for filtering
filtered_genes = RNA_dataset.iloc[1:, :][row_sums > threshold]  # Use the same row index


# Align overlapping_TF_motif_binding_df to the filtered RNA dataset
aligned_TF_binding = overlapping_TF_motif_binding_df[
    overlapping_TF_motif_binding_df["Source"].isin(aligned_RNA["Genes"])
]

# Find common indices
common_indices = aligned_RNA.index.intersection(aligned_TF_binding.index)

# Filter both DataFrames
aligned_RNA = aligned_RNA.loc[common_indices].set_index("Genes", drop=True)
aligned_TF_binding = aligned_TF_binding.loc[common_indices]

# Extract expression matrix (genes x cells)
expression_matrix = aligned_RNA.iloc[:, 1:].values

# Extract motif scores
motif_scores = aligned_TF_binding["Score"].values.reshape(-1, 1)

# Perform element-wise multiplication
weighted_expression = aligned_RNA.values * motif_scores

# Create a DataFrame for the weighted expression
weighted_expression_df = pd.DataFrame(
    weighted_expression,
    index=aligned_RNA.index,
    columns=aligned_RNA.columns
)

print(f'Number of TFs = {weighted_expression_df.shape[0]}')
print(f'Number of TGs = {weighted_expression_df.shape[1]}')

plt.figure(figsize=(15, 10))
sns.heatmap(weighted_expression_df, cmap="viridis", cbar=True)
plt.title("Cell Expressions Heatmap")
plt.xlabel("Cells")
plt.ylabel("Genes")
plt.savefig("TF_score_heatmap.png", dpi=200)

all_values = weighted_expression_df.values.flatten()

nonzero_values = all_values[all_values > 0]
log2_values = np.log2(nonzero_values)

plt.figure(figsize=(10, 6))
plt.hist(log2_values, bins=50, color="blue", alpha=0.7)
plt.title("Distribution of Expression Values")
plt.xlabel("Expression Value")
plt.ylabel("Frequency")
plt.savefig("TF_score_distribution.png", dpi=200)

print(f'Mean gene score = {weighted_expression_df.mean()}')