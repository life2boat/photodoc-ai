import * as faceapi from 'face-api.js';

const MODEL_URL = '/models';
let modelsPromise = null;

const loadModels = () => {
  if (!modelsPromise) {
    modelsPromise = Promise.all([
      faceapi.nets.ssdMobilenetv1.loadFromUri(MODEL_URL),
      faceapi.nets.faceLandmark68Net.loadFromUri(MODEL_URL),
    ]);
  }
  return modelsPromise;
};

const getCenter = (points, width, height) => ({
  x: points.reduce((sum, p) => sum + p.x, 0) / points.length / width,
  y: points.reduce((sum, p) => sum + p.y, 0) / points.length / height,
});

export const detectFacePoints = async (imageElement) => {
  try {
    await loadModels();
    const natWidth = imageElement.naturalWidth;
    const natHeight = imageElement.naturalHeight;
    
    if (!natWidth || !natHeight) {
      throw new Error('Изображение еще не загрузилось');
    }
    
    const detection = await faceapi.detectSingleFace(imageElement).withFaceLandmarks();
    if (!detection) return null;
    
    const landmarks = detection.landmarks.positions;
    const leftEye = getCenter(landmarks.slice(36, 42), natWidth, natHeight);
    const rightEye = getCenter(landmarks.slice(42, 48), natWidth, natHeight);
    
    // Валидация наклона головы (Roll)
    const eyeAngleDeg = (Math.atan2(rightEye.y - leftEye.y, rightEye.x - leftEye.x) * 180) / Math.PI;
    if (Math.abs(eyeAngleDeg) > 6) {
      throw new Error(`Фото не подходит: голова наклонена слишком сильно (${Math.abs(eyeAngleDeg).toFixed(1)}°). Максимум 6°. Пожалуйста, загрузите фото анфас.`);
    }
    
    const chin = { x: landmarks[8].x / natWidth, y: landmarks[8].y / natHeight };
    const box = detection.detection.box;
    const crown = {
      x: (leftEye.x + rightEye.x) / 2,
      y: Math.max(0, (box.top / natHeight) + 0.04),
    };
    
    const jawWidth = Math.abs(landmarks[16].x - landmarks[0].x) / natWidth;
    
    return { leftEye, rightEye, chin, crown, eyeAngleDeg, jawWidth };
  } catch (error) {
    console.error('Ошибка в модуле детекции:', error);
    throw error;
  }
};