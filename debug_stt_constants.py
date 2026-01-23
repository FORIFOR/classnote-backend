
from google.cloud.speech_v2.types import cloud_speech as cs
try:
    print(f"LINEAR16: {cs.ExplicitDecodingConfig.AudioEncoding.LINEAR16}")
except AttributeError:
    try:
        print(f"LINEAR_16: {cs.ExplicitDecodingConfig.AudioEncoding.LINEAR_16}")
    except AttributeError:
        print("Neither LINEAR16 nor LINEAR_16 found in AudioEncoding")
        print(dir(cs.ExplicitDecodingConfig.AudioEncoding))
