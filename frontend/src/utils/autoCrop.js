export const generateAutoCrop = async (imageSrc, pointsPercent, formatWidth, formatHeight, targetDpi = 300) => {
  const img = new Image();
  img.crossOrigin = "anonymous";
  img.src = imageSrc;
  await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; });

  const w = img.naturalWidth;
  const h = img.naturalHeight;
  
  // Константы ГОСТ (мм)
  const GOST = {
    headMin: 32, headMax: 36, headTarget: 34,
    faceMin: 16, faceMax: 22, faceTarget: 19,
    topMargin: 0.11 // 11% высоты (~5мм)
  };

  const mmToPx = targetDpi / 25.4;
  const targetHeight = Math.round(formatHeight * mmToPx);
  const targetWidth = Math.round(formatWidth * mmToPx);

  // Текущие замеры в пикселях исходника
  const currentHeadPx = Math.hypot(
    (pointsPercent.chin.x - pointsPercent.crown.x) * w, 
    (pointsPercent.chin.y - pointsPercent.crown.y) * h
  );
  const currentFacePx = (pointsPercent.jawWidth || 0.4) * w;

  // 1. Расчет масштаба по высоте головы (цель 34мм)
  let scale = (GOST.headTarget * mmToPx) / currentHeadPx;

  // 2. Проверка ширины лица после масштабирования
  const projectedFaceMm = (currentFacePx * scale) / mmToPx;
  
  // Если лицо слишком широкое (>22мм), уменьшаем масштаб до лимита
  if (projectedFaceMm > GOST.faceMax) {
    scale = (GOST.faceMax * mmToPx) / currentFacePx;
  } 
  // Если лицо слишком узкое (<16мм), увеличиваем до лимита
  else if (projectedFaceMm < GOST.faceMin) {
    scale = (GOST.faceMin * mmToPx) / currentFacePx;
  }

  const canvas = document.createElement('canvas');
  canvas.width = targetWidth;
  canvas.height = targetHeight;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, targetWidth, targetHeight);

  // Отрисовка с учетом Smart Scale
  const eyeCenterX = ((pointsPercent.leftEye.x + pointsPercent.rightEye.x) / 2) * w;
  const headCenterY = ((pointsPercent.crown.y + pointsPercent.chin.y) / 2) * h;
  const angle = Math.atan2(
    (pointsPercent.rightEye.y - pointsPercent.leftEye.y) * h, 
    (pointsPercent.rightEye.x - pointsPercent.leftEye.x) * w
  );

  const targetHeadHeightPx = currentHeadPx * scale;
  const targetHeadCenterY = (targetHeight * GOST.topMargin) + (targetHeadHeightPx / 2);

  ctx.save();
  ctx.translate(targetWidth / 2, targetHeadCenterY);
  ctx.rotate(-angle);
  ctx.scale(scale, scale);
  ctx.drawImage(img, -eyeCenterX, -headCenterY);
  ctx.restore();

  return new Promise(resolve => {
    canvas.toBlob(blob => resolve(blob ? URL.createObjectURL(blob) : null), 'image/jpeg', 1.0);
  });
};