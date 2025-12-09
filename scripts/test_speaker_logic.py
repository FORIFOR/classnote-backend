from datetime import datetime
import json

def _process_speakers(speakers_data: list) -> list:
    """話者情報の display_name を補完する"""
    if not speakers_data:
        return []
    processed = []
    for s in speakers_data:
        spk = dict(s)
        # 必須フィールドの確認と補完
        label = spk.get("label") or spk.get("cluster") or "?"
        spk["label"] = label
        
        if "id" not in spk:
            spk["id"] = spk.get("speaker_id") or f"spk_{label}"
            
        # displayName の補完
        if not spk.get("displayName"):
            spk["displayName"] = f"話者{label}"
            
        processed.append(spk)
    return processed

def test_logic():
    print("Testing _process_speakers...")
    
    # Case 1: Empty
    assert _process_speakers([]) == []
    
    # Case 2: Full data
    input2 = [{"id": "s1", "label": "A", "displayName": "Alice", "colorHex": "#FFF"}]
    assert _process_speakers(input2)[0]["displayName"] == "Alice"
    
    # Case 3: Missing displayName (Fallback)
    input3 = [{"id": "s2", "label": "B"}]
    res3 = _process_speakers(input3)
    assert res3[0]["displayName"] == "話者B"
    print(f"Case 3 (Fallback): {res3[0]}")
    
    # Case 4: Missing label (Fallback)
    input4 = [{"cluster": "C"}]
    res4 = _process_speakers(input4)
    assert res4[0]["label"] == "C"
    assert res4[0]["displayName"] == "話者C"
    assert res4[0]["id"] == "spk_C"
    print(f"Case 4 (Label/ID Fallback): {res4[0]}")
    
    print("All tests passed!")

if __name__ == "__main__":
    test_logic()
