from scipy.stats import wilcoxon

from lexical_overlap_computation import compute_overlaps
import os
import json
from collections import Counter
import statistics
import pandas as pd
from sklearn.metrics import cohen_kappa_score
from scipy.stats import spearmanr

import networkx as nx
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
import pickle

# Utilities
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_table(data, path):
    with open(path, "w", encoding="utf-8") as f:
        for k, v in data.items():
            f.write(f"{k}:\t{v}\n")

# Learning object quality assessment functions
def evaluate_learning_objects(learning_objects, output_dir, comparison_learning_objects=None, comparison_learning_objects_2=None):
    overlaps = [
        lo["lexical_overlap"]
        for lo in learning_objects
        if "lexical_overlap" in lo
    ]

    if comparison_learning_objects is not None and comparison_learning_objects_2 is not None:
        overlaps_B = [
            lo["lexical_overlap"]
            for lo in comparison_learning_objects
            if "lexical_overlap" in lo
        ]
        overlaps_C = [
            lo["lexical_overlap"]
            for lo in comparison_learning_objects_2
            if "lexical_overlap" in lo
        ]

    stats = {
        "count": len(overlaps),
        "mean_overlap": statistics.mean(overlaps),
        "std_overlap": statistics.stdev(overlaps) if len(overlaps) > 1 else 0,
        "min_overlap": min(overlaps),
        "max_overlap": max(overlaps),
    }

    print("\nLexical Overlap Stats")
    for k, v in stats.items():
        print(f"{k}: {v}")
    save_table(stats, os.path.join(output_dir, "learning_objects.txt"))

    #Histogram
    plt.figure()
    #plt.hist(overlaps, bins=6)
    bins = np.linspace(0.5, 1.0, 30)
    if comparison_learning_objects is not None and comparison_learning_objects_2 is not None:
        plt.hist(overlaps, bins=bins, alpha=0.5, label="Practical C")
        plt.hist(overlaps_B, bins=bins, alpha=0.5, label="Data Science")
        plt.hist(overlaps_C, bins=bins, alpha=0.5, label="C Programming")
    else:
        plt.hist(overlaps, bins=bins)
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.legend()
    plt.title("Lexical Overlap Histogram")
    plt.xlabel("Lexical Overlap")
    plt.ylabel("Frequency")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "lexical_overlap_hist.png"))
    plt.close()

    # LO type distribution
    types = Counter(lo["type"] for lo in learning_objects)

    plt.figure()
    plt.bar(types.keys(), types.values())
    plt.title("Learning Object Type Distribution")
    plt.xticks(rotation=45)
    plt.ylabel("Count")
    plt.savefig(os.path.join(output_dir, "lo_type_distribution.png"))
    plt.close()

    save_table(dict(types), os.path.join(output_dir, "lo_type_counts.txt"))

# Concept graph evaluation
def evaluate_concept_graph(G, output_dir):
    stats = {

        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "density": nx.density(G),
        "avg_degree": sum(dict(G.degree()).values()) / G.number_of_nodes(),
        "connected_components": nx.number_connected_components(G),
    }

    print("\nConcept Graph Stats")
    for k, v in stats.items():
        print(f"{k}: {v}")
    save_table(stats, os.path.join(output_dir, "graph_stats.txt"))

    # Degree distribution
    degrees = [d for _, d in G.degree()]

    plt.figure()
    plt.hist(degrees, bins=15)
    plt.style.use("seaborn-v0_8-whitegrid")
    plt.title("Concept Degree Distribution")
    plt.xlabel("Degree")
    plt.ylabel("Frequency")
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "degree_distribution.png"))
    plt.close()

    #Centrality Distribution
    centrality = nx.degree_centrality(G).values()

    plt.figure()
    plt.hist(list(centrality), bins=30)
    plt.style.use("seaborn-v0_8-whitegrid")  # cleaner background
    plt.title("Centrality Distribution")
    plt.xlabel("Centrality")
    plt.ylabel("Frequency")
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "centrality_distribution.png"))
    plt.close()

# Learning Path Evaluation
def evaluate_learning_paths(KG, output_dir):
    stats = {
        "nodes": KG.number_of_nodes(),
        "edges": KG.number_of_edges(),
        "is_dag": nx.is_directed(KG),
    }
    if stats["is_dag"]:
        stats["longest_path_length"] = nx.dag_longest_path_length(KG)
    else:
        stats["longest_path_length"] = "N/A"

    roots = [n for n in KG.nodes() if KG.in_degree(n) == 0]
    stats["root_nodes"] = len(roots)

    print("\nKnowledge Graph Stats")
    for k, v in stats.items():
        print(k, ":", v)

    save_table(stats, os.path.join(output_dir, "learning_path_stats.txt"))

    depths = {}
    for root in roots:
        lengths = nx.single_source_shortest_path_length(KG, root)
        for node, depth in lengths.items():
            depths[node] = max(depths.get(node, 0), depth)

    plt.figure()
    plt.hist(list(depths.values()), bins=20)
    plt.style.use("seaborn-v0_8-whitegrid")  # cleaner background
    plt.title("Learning Path Depth Distribution")
    plt.xlabel("Depth")
    plt.ylabel("Frequency")
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "learning_depth_distribution.png"))
    plt.close()

# Evaluations Stats
def compute_numeric_stats(data, key):
    values = [item[key] for item in data if key in item]

    return {
        "mean": round(statistics.mean(values), 3),
        "std": round(statistics.stdev(values), 3) if len(values) > 1 else 0,
        "min": min(values),
        "max": max(values),
    }


def compute_boolean_rate(data, key, positive_value):
    total = len(data)
    positives = sum(1 for item in data if item.get(key) == positive_value)

    return {
        "count": positives,
        "total": total,
        "rate": round(positives / total, 3)
    }

def analyze_evaluations(evaluations):

    metrics = [
        "faithfulness",
        "clarity",
        "hint_quality",
        "pedagogical_value",
    ]

    results = {}

    # Numeric metrics
    for metric in metrics:
        results[metric] = compute_numeric_stats(evaluations, metric)

    # Atomicity
    results["atomicity"] = compute_boolean_rate(
        evaluations,
        "atomic",
        "yes"
    )

    # Hallucination rate
    hallucinations = sum(
        1 for item in evaluations
        if item.get("hallucination_detected") == "yes"
    )

    results["hallucination_rate"] = {
        "count": hallucinations,
        "total": len(evaluations),
        "rate": round(hallucinations / len(evaluations), 3)
    }

    return results

def print_tables(results):

    print("\n=== Learning Object Quality Metrics ===")
    print(f"{'Metric':<22}{'Mean':<8}{'Std':<8}{'Min':<6}{'Max':<6}")

    for metric in [
        "faithfulness",
        "clarity",
        "hint_quality",
        "pedagogical_value",
    ]:
        r = results[metric]
        print(f"{metric:<22}{r['mean']:<8}{r['std']:<8}{r['min']:<6}{r['max']:<6}")

    print("\n=== Atomicity ===")
    a = results["atomicity"]
    print(f"Atomic objects: {a['count']} / {a['total']} ({a['rate']*100:.1f}%)")

    print("\n=== Hallucination Detection ===")
    h = results["hallucination_rate"]
    print(f"Hallucinations: {h['count']} / {h['total']} ({h['rate']*100:.1f}%)")

def save_summary(results, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

# Graph Builder fallback
def build_concept_graph(units):
    G = nx.Graph()
    for unit in units:
        concepts = unit.get("normalized_concepts", [])
        for c in concepts:
            G.add_node(c)

        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                if G.has_edge(concepts[i], concepts[j]):
                    G[concepts[i]][concepts[j]]["weight"] += 1
                else:
                    G.add_edge(concepts[i], concepts[j], weight=1)
    return G

def build_dummy_knowledge_graph(learning_objects):
    """
    Fallback if KG not saved yet.
    Creates empty graph with nodes only.
    """
    KG = nx.DiGraph()
    for lo in learning_objects:
        KG.add_node(lo["id"])
    return KG

def compute_cohens_kappa(evaluation_file: str, metrics: list = None):
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    if metrics is None:
        metrics = ["faithfulness", "question_clarity"]
    models = list(df["model"].unique())

    print("\n=== Cohen's Kappa Inter-Model Agreement ===")
    from itertools import combinations
    for model_a, model_b in combinations(models, 2):
        short_a = model_a.split("/")[-1]
        short_b = model_b.split("/")[-1]
        print(f"\nComparing: {short_a} vs {short_b}")
        for metric in metrics:
            if metric not in df.columns:
                continue
            scores_a = df[df["model"] == model_a][metric].dropna().values
            scores_b = df[df["model"] == model_b][metric].dropna().values
            if len(scores_a) == 0 or len(scores_b) == 0:
                continue
            kappa = cohen_kappa_score(scores_a, scores_b)
            print(f"  {metric:<22} {kappa:.3f}")


def weighted_kappa(evaluation_file: str, metrics: list = None):
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    if metrics is None:
        metrics = ["faithfulness", "question_clarity"]
    models = list(df["model"].unique())

    print("\n=== Weighted Cohen's Kappa ===")
    from itertools import combinations
    for model_a, model_b in combinations(models, 2):
        short_a = model_a.split("/")[-1]
        short_b = model_b.split("/")[-1]
        print(f"\nComparing: {short_a} vs {short_b}")
        for metric in metrics:
            if metric not in df.columns:
                continue
            a = df[df["model"] == model_a][metric].dropna().values
            b = df[df["model"] == model_b][metric].dropna().values
            if len(a) == 0 or len(b) == 0:
                continue
            kappa = cohen_kappa_score(a, b, weights="quadratic")
            print(f"  {metric:<22} {kappa:.3f}")


def wilcoxon_model_comparison(evaluation_file: str, metrics: list = None):
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    if metrics is None:
        metrics = ["faithfulness", "question_clarity"]
    models = list(df["model"].unique())

    print("\n=== Wilcoxon Signed-Rank Test ===")
    from itertools import combinations
    for model_a, model_b in combinations(models, 2):
        short_a = model_a.split("/")[-1]
        short_b = model_b.split("/")[-1]
        print(f"\nComparing: {short_a} vs {short_b}")
        for metric in metrics:
            if metric not in df.columns:
                continue
            a = df[df["model"] == model_a][metric].dropna().values
            b = df[df["model"] == model_b][metric].dropna().values
            if len(a) == 0 or len(b) == 0:
                continue
            try:
                stat, p = wilcoxon(a, b)
                print(f"  {metric:<22} stat={stat:.3f} p={p:.4f}")
            except ValueError as e:
                print(f"  {metric:<22} skipped ({e})")


def spearman_agreement(evaluation_file: str, metrics: list = None):
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    if metrics is None:
        metrics = ["faithfulness", "question_clarity"]
    models = list(df["model"].unique())

    print("\n=== Spearman Rank Correlation ===")
    from itertools import combinations
    for model_a, model_b in combinations(models, 2):
        short_a = model_a.split("/")[-1]
        short_b = model_b.split("/")[-1]
        print(f"\nComparing: {short_a} vs {short_b}")
        for metric in metrics:
            if metric not in df.columns:
                continue
            a = df[df["model"] == model_a][metric].dropna().values
            b = df[df["model"] == model_b][metric].dropna().values
            if len(a) == 0 or len(b) == 0:
                continue
            rho, p = spearmanr(a, b)
            print(f"  {metric:<22} rho={rho:.3f} p={p:.4f}")


def print_binary_agreement(evaluation_file: str):
    """Agreement on binary metrics across models."""
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    binary_metrics = ["self_contained", "atomic"]
    models = list(df["model"].unique())

    print("\n=== Binary Metric Agreement (% yes per model) ===")
    for metric in binary_metrics:
        if metric not in df.columns:
            continue
        print(f"\n{metric}:")
        for model in models:
            model_data = df[df["model"] == model]
            yes = (model_data[metric] == "yes").sum()
            total = len(model_data)
            print(f"  {model.split('/')[-1]:<25} {yes}/{total} ({yes/total*100:.1f}%)")

    print("\n=== Distractor Plausibility ===")
    if "distractor_plausibility" in df.columns:
        for model in models:
            vals = df[df["model"] == model]["distractor_plausibility"].dropna()
            if len(vals) > 0:
                print(f"  {model.split('/')[-1]:<25} mean={vals.mean():.3f} n={len(vals)}")


def generate_combined_boxplots(evaluation_file: str,
                                output_path="./output/evaluation_boxplots.png",
                                metrics: list = None):
    with open(evaluation_file) as f:
        data = json.load(f)
    df = pd.DataFrame(data)

    if metrics is None:
        metrics = ["faithfulness", "question_clarity"]

    # Only plot metrics that exist and are numeric
    metrics = [m for m in metrics if m in df.columns]
    n = len(metrics)
    cols = 2
    rows = (n + 1) // cols

    fig, axes = plt.subplots(rows, cols, figsize=(10, 4 * rows))
    axes = axes.flatten() if n > 1 else [axes]

    for i, metric in enumerate(metrics):
        sns.boxplot(data=df, x="model", y=metric, ax=axes[i])
        axes[i].set_title(metric.replace("_", " ").title())
        axes[i].set_xlabel("")
        axes[i].set_ylabel("Score")
        axes[i].set_xticks(axes[i].get_xticks())
        axes[i].set_xticklabels([
            label.get_text().split("/")[-1][:15]
            for label in axes[i].get_xticklabels()
        ], rotation=15, ha='right')

    # Hide unused subplots
    for i in range(n, len(axes)):
        axes[i].set_visible(False)

    plt.tight_layout()
    plt.savefig(output_path, dpi=300)
    plt.close()
    print(f"[Saved] {output_path}")

if __name__ == "__main__":
    # --- New metrics evaluations
    new_eval_file = "./output/evaluations_new_metrics.json"
    #new_eval_file = "./output/evaluations_sample_30.json"
    new_metrics = ["faithfulness", "question_clarity", "hint_quality"]

    compute_cohens_kappa(new_eval_file, metrics=new_metrics)
    weighted_kappa(new_eval_file, metrics=new_metrics)
    spearman_agreement(new_eval_file, metrics=new_metrics)
    wilcoxon_model_comparison(new_eval_file, metrics=new_metrics)
    print_binary_agreement(new_eval_file)
    generate_combined_boxplots(new_eval_file, output_path="./output/evaluation_sample_30_boxplots.png", metrics=new_metrics)

    print("\nEvaluation complete.")