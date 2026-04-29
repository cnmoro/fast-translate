#!/usr/bin/env python3
"""
Script to translate GSM8K samples from English to Portuguese and identify errors.
"""
import json
from pathlib import Path
from datasets import load_dataset

from fast_translate import Translator

# Import functions directly from the file
import sys
sys.path.insert(0, '/mnt/dados3/translate')
from translation_quality_enhanced import qa_failures, post_edit_portuguese


def main():
    # Load GSM8K dataset
    print("Loading GSM8K dataset...")
    dataset = load_dataset("openai/gsm8k", "main", split="train")
    print(f"Total samples available: {len(dataset)}")
    
    # Limit to at least 1000 samples
    limit = min(len(dataset), 1000)
    print(f"Processing {limit} samples...")
    
    # Create translator
    with Translator() as tr:
        errors = []
        total = 0
        failed = 0
        
        # Process samples in batches to avoid memory issues
        for idx in range(limit):
            row = dataset[idx]
            question = str(row.get("question", "")).strip()
            answer = str(row.get("answer", "")).strip()
            
            if not question:
                continue
            
            total += 1
            
            # Translate from English to Portuguese
            translated = tr.translate(question, direction="en-pt")
            
            # Apply post-processing fixes
            translated_fixed = post_edit_portuguese(translated)
            
            # Check for errors on raw translation
            failures_raw = qa_failures(question, translated)
            # Check for errors on post-processed translation
            failures_fixed = qa_failures(question, translated_fixed)
            
            # Track both raw and fixed errors
            raw_fail_count = len(failures_raw)
            fixed_fail_count = len(failures_fixed)
            
            # If translation had errors, record it
            if failures_raw:
                failed += 1
                error_record = {
                    "index": idx,
                    "question": question,
                    "translated_raw": translated,
                    "translated_fixed": translated_fixed,
                    "failures_raw": failures_raw,
                    "failures_fixed": failures_fixed,
                    "improved": failures_fixed is None or len(failures_fixed) == 0,
                    "answer": answer,
                }
                errors.append(error_record)
                
                # Print first 10 errors for manual review
                if idx < 10:
                    print(f"\n=== Error #{idx + 1} ===")
                    print(f"Question: {question}")
                    print(f"Translated (raw): {translated}")
                    print(f"Translated (fixed): {translated_fixed}")
                    print(f"Raw failures: {failures_raw}")
                    print(f"Fixed failures: {failures_fixed}")
                    print(f"Answer: {answer}\n")
        
        print(f"\n{'='*60}")
        print(f"Processing complete!")
        print(f"Total samples processed: {total}")
        print(f"Samples with errors (raw): {failed}")
        print(f"Raw error rate: {100*failed/total:.2f}%")
        
        # Count how many were fixed by post-processing
        fixed_count = len([e for e in errors if e.get("improved")])
        print(f"Samples fixed by post-processing: {fixed_count}")
        print(f"Remaining errors after fix: {failed - fixed_count}")
        print(f"Post-processing improvement: {100*fixed_count/failed:.1f}%" if failed > 0 else "N/A")
        
        # Save errors to JSON file
        error_file = Path("/mnt/dados3/translate/library/artifacts/gsm8k_translation_errors.json")
        error_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Save with indentation for readability
        with open(error_file, "w", encoding="utf-8") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
        
        print(f"\nErrors saved to: {error_file}")
        print(f"JSON file size: {error_file.stat().st_size / 1024:.1f} KB")
        
        # Also save metrics
        error_types_raw = {
            "count": len([e for e in errors if any("english_leak" in f for f in e["failures_raw"])]),
            "unbalanced_quotes": len([e for e in errors if any("unbalanced_quotes" in f for f in e["failures_raw"])]),
            "parenthesis_mismatch": len([e for e in errors if any("parenthesis_mismatch" in f for f in e["failures_raw"])]),
            "number_mismatch": len([e for e in errors if any("number_mismatch" in f for f in e["failures_raw"])]),
            "missing_space_after_punct": len([e for e in errors if any("missing_space_after_punct" in f for f in e["failures_raw"])]),
            "space_before_punct": len([e for e in errors if any("space_before_punct" in f for f in e["failures_raw"])]),
            "double_spaces": len([e for e in errors if any("double_spaces" in f for f in e["failures_raw"])]),
        }
        
        metrics = {
            "total_samples": total,
            "failed_samples_raw": failed,
            "failed_samples_after_fix": failed - fixed_count,
            "error_rate_raw": 100 * failed / total if total > 0 else 0,
            "error_rate_after_fix": 100 * (failed - fixed_count) / total if total > 0 else 0,
            "samples_fixed_by_post_processing": fixed_count,
            "improvement_rate": 100 * fixed_count / failed if failed > 0 else 0,
            "error_types_raw": error_types_raw,
        }
        
        metrics_file = Path("/mnt/dados3/translate/library/artifacts/gsm8k_translation_metrics.json")
        with open(metrics_file, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        
        print(f"Metrics saved to: {metrics_file}")
        
        return failed


if __name__ == "__main__":
    errors_count = main()
    print(f"\nTotal errors found: {errors_count}")
