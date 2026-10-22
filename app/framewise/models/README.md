# Noise-suppression models

RNNoise models from https://github.com/GregorR/rnnoise-models, used by
ffmpeg's `arnndn` filter to remove background noise (keyboard, fan, hum)
from the microphone track.

- `voice.rnnn`: "beguiling-drafter" (voice, with recording noise). Normal.
- `voice-strong.rnnn`: "somnolent-hogwash" (speech, with recording noise). Strong.

The upstream README states that the models are not creative work and are
not subject to copyright.

# Camera effect models

Used by the camera bubble's effects (background blur, smooth skin, face light),
run with OpenCV's DNN module (python3-opencv).

- `face_detection_yunet_2023mar.onnx`: YuNet face detector, from the OpenCV
  model zoo (https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet).
  MIT License. Finds faces with eyes, nose and mouth corners.
- `selfie_segmentation.onnx`: MediaPipe Selfie Segmentation (Google), ONNX
  export from https://huggingface.co/onnx-community/mediapipe_selfie_segmentation.
  Apache License 2.0. Separates the person from the background (256x256 input).
