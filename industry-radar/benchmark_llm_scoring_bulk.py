import json
import time
import os
import sys
import yaml
import concurrent.futures

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from score import score_articles_batch

def run_bulk_determinism_benchmark():
    md_testset_path = os.path.join(os.path.dirname(__file__), "md_testset.json")
    with open(md_testset_path, "r", encoding="utf-8") as f:
        md_articles = json.load(f)

    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    test_set = []

    for idx, a in enumerate(md_articles):
        # The structure is already perfect for scoring
        # We just need to give it an id
        article = {
            "id": idx,
            "link": a.get("link", ""),
            "title": a.get("title", ""), # using translated title
            "summary": a.get("summary", ""),
            "historical_i_score": a.get("historical_i_score", 0),
            "historical_t_score": a.get("historical_t_score", 0)
        }
        test_set.append(article)

    print(f"Starting bulk test with {len(test_set)} articles from Markdown reports")

    # Run scoring 1
    print("Executing Run 1...")
    results1 = score_articles_batch(test_set, config)

    # Run scoring 2
    print("Executing Run 2...")
    results2 = score_articles_batch(test_set, config)

    if "results" not in results1 or "results" not in results2:
        print("Failed to run batch scoring!")
        return

    # Analyze errors
    i_errors = []
    t_errors = []

    max_dev = 0
    worst_case = None

    print("\n--- Test Results (Run 1 vs Run 2) ---")

    res1_map = {r.get("id"): r for r in results1["results"]}
    res2_map = {r.get("id"): r for r in results2["results"]}

    for idx in res1_map:
        orig = next((a for a in test_set if a["id"] == idx), None)
        if not orig: continue
        if idx not in res2_map: continue

        r1 = res1_map[idx]
        r2 = res2_map[idx]

        new1_i = float(r1.get("innovation_score", 0))
        new1_t = float(r1.get("traffic_score", 0))

        new2_i = float(r2.get("innovation_score", 0))
        new2_t = float(r2.get("traffic_score", 0))

        i_err = abs(new1_i - new2_i)
        t_err = abs(new1_t - new2_t)

        i_errors.append(i_err)
        t_errors.append(t_err)

        dev = max(i_err, t_err)
        if dev > max_dev:
            max_dev = dev
            worst_case = {
                "title": orig["title"],
                "r1_i": new1_i, "r1_t": new1_t,
                "r2_i": new2_i, "r2_t": new2_t,
                "dev": dev
            }

        print(f"[{'HIGH' if orig['historical_i_score']>=7 or orig['historical_t_score']>=7 else 'LOW'}] {orig['title'][:30]}... | R1(I:{new1_i:.1f}, T:{new1_t:.1f}) -> R2(I:{new2_i:.1f}, T:{new2_t:.1f}) | Dev: {dev:.1f}")

    mae_i = sum(i_errors) / len(i_errors) if i_errors else 0
    mae_t = sum(t_errors) / len(t_errors) if t_errors else 0
    mae_overall = (mae_i + mae_t) / 2

    print("\n--- Summary ---")
    print(f"MAE Innovation: {mae_i:.2f}")
    print(f"MAE Traffic:    {mae_t:.2f}")
    print(f"MAE Overall:    {mae_overall:.2f}")
    print(f"Max Deviation:  {max_dev:.2f}")
    if worst_case:
        print(f"Worst Case: {worst_case['title'][:40]} (R1 I:{worst_case['r1_i']:.1f}, T:{worst_case['r1_t']:.1f} -> R2 I:{worst_case['r2_i']:.1f}, T:{worst_case['r2_t']:.1f})")

    if mae_overall > 1.0 or max_dev > 1.5:
        print("\n❌ DETERMINISM TEST FAILED: Variances exceed acceptable threshold!")
        sys.exit(1)
    else:
        print("\n✅ DETERMINISM TEST PASSED!")
        sys.exit(0)

if __name__ == "__main__":
    run_bulk_determinism_benchmark()
