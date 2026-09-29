# ============================================================
#  inspect_inputs.py — Quick Column Inspector
# ============================================================
#  Run this first after placing your Excel files in /input
#  It will print all column names + sample rows so you can
#  verify the data before building the output logic.
# ============================================================

import pandas as pd
from config import INPUT_FILES, SHEET_NAMES


def inspect(name: str, filepath: str, sheet):
    print(f"\n{'='*65}")
    print(f"  📄  {name.upper().replace('_', ' ')}")
    print(f"  Path: {filepath}")
    print(f"{'='*65}")
    
    try:
        df = pd.read_excel(filepath, sheet_name=sheet, nrows=200)
        df.columns = df.columns.str.strip()
        
        print(f"\n  SHAPE : {df.shape[0]} rows × {df.shape[1]} columns")
        print(f"\n  COLUMNS ({len(df.columns)}):")
        for i, col in enumerate(df.columns, 1):
            dtype = df[col].dtype
            nulls = df[col].isna().sum()
            sample = df[col].dropna().iloc[0] if df[col].dropna().shape[0] > 0 else "—"
            print(f"    {i:>3}. {col:<40} [{dtype}]  nulls={nulls}  sample={repr(sample)}")
        
        print(f"\n  FIRST 3 ROWS:")
        print(df.head(3).to_string(index=False))
    
    except FileNotFoundError:
        print(f"  ❌ File not found: {filepath}")
        print(f"     → Place the file there and re-run this script.")
    except Exception as e:
        print(f"  ❌ Error reading file: {e}")


if __name__ == "__main__":
    print("\n" + "🔍 INPUT FILE INSPECTION ".center(65, "="))
    for key, path in INPUT_FILES.items():
        inspect(key, path, SHEET_NAMES.get(key, 0))
    print("\n" + "="*65)
    print("  ✅ Inspection complete. Share the column names with Antigravity")
    print("     to build the output processing logic.")
    print("="*65 + "\n")
