#!/bin/bash -l

#SBATCH --partition compute
#SBATCH --nodes=1
#SBATCH --cpus-per-task 2
#SBATCH --mem 32G
#SBATCH --output=LOGS/apply_trained_rf_model.log
#SBATCH --error=LOGS/apply_trained_rf_model.err

source activate my_env

PROJECT_DIR="/gpfs/Labs/Uzun/SCRIPTS/PROJECTS/2024.SINGLE_CELL_GRN_INFERENCE.MOELLER/"

MESC_OUTPUT="$PROJECT_DIR/output/mESC/filtered_L2_E7.5_rep1/"
MESC_GROUND_TRUTH="$PROJECT_DIR/ground_truth_files/RN111.tsv"

MACROPHAGE_OUTPUT="$PROJECT_DIR/output/macrophage"
MACROPHAGE_GROUND_TRUTH="$PROJECT_DIR/ground_truth_files/RN204_macrophage_ground_truth.tsv"

K562_OUTPUT="$PROJECT_DIR/output/K562/K562_human_filtered"
K562_GROUND_TRUTH="$PROJECT_DIR/ground_truth_files/RN117_ChIPSeq_PMID37486787_Human_K562.tsv"


determine_num_cpus() {
    if [ -z "${SLURM_CPUS_PER_TASK:-}" ]; then
        if command -v nproc &> /dev/null; then
            TOTAL_CPUS=$(nproc --all)
            case $TOTAL_CPUS in
                [1-15]) IGNORED_CPUS=1 ;;  # Reserve 1 CPU for <=15 cores
                [16-31]) IGNORED_CPUS=2 ;; # Reserve 2 CPUs for <=31 cores
                *) IGNORED_CPUS=4 ;;       # Reserve 4 CPUs for >=32 cores
            esac
            NUM_CPU=$((TOTAL_CPUS - IGNORED_CPUS))
            echo "[INFO] Running locally. Detected $TOTAL_CPUS CPUs, reserving $IGNORED_CPUS for system tasks. Using $NUM_CPU CPUs."
        else
            NUM_CPU=1  # Fallback
            echo "[INFO] Running locally. Unable to detect CPUs, defaulting to $NUM_CPU CPU."
        fi
    else
        NUM_CPU=${SLURM_CPUS_PER_TASK}
        echo "[INFO] Running on SLURM. Number of CPUs allocated: ${NUM_CPU}"
    fi
}

determine_num_cpus

# ======== XGBOOST MODEL ========
# mESC vs K562
python3 "$PROJECT_DIR/src/testing_scripts/apply_trained_xgboost.py" \
    --output_dir "$K562_OUTPUT" \
    --model "$K562_OUTPUT/trained_xgboost_model_full.pkl" \
    --target  "$K562_OUTPUT/inferred_network_raw.csv"\
    --save_name "full_network"

