// Единый источник правды для размеров рамок (в миллиметрах)
export const FRAME_SPECS = {
  classic600: {
    name: 'Classic 600',
    paperW: 88,
    paperH: 107,
    photoSize: 79,
    borderTopSide: 4.5, 
    bgColor: '#f3f4f6', 
  }
};

/**
 * Генерирует фото в стиле Polaroid с поддержкой ручного смещения (Manual Crop) и зума
 */
export const generatePolaroid = async (
  imageUrl, 
  frameType = 'classic600', 
  targetDpi = 300, 
  cropConfig = { x: 0, y: 0, zoom: 1 } 
) => {
  const spec = FRAME_SPECS[frameType];
  const mmToPx = targetDpi / 25.4;
  
  const canvasW = Math.round(spec.paperW * mmToPx); 
  const canvasH = Math.round(spec.paperH * mmToPx); 
  const photoSizePx = Math.round(spec.photoSize * mmToPx);
  const offsetPx = Math.round(spec.borderTopSide * mmToPx);

  const canvas = document.createElement('canvas');
  canvas.width = canvasW;
  canvas.height = canvasH;
  const ctx = canvas.getContext('2d');
  
  ctx.fillStyle = spec.bgColor;
  ctx.fillRect(0, 0, canvasW, canvasH);

  ctx.fillStyle = '#111111';
  ctx.fillRect(offsetPx, offsetPx, photoSizePx, photoSizePx);

  const img = new Image();
  img.src = imageUrl;
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
  });

  // Математика переноса экранных координат на оригинал
  const uiBoxSize = 288; 
  const minSide = Math.min(img.width, img.height);
  const baseScale = uiBoxSize / minSide; 
  const cropSizeOnOriginal = minSide / cropConfig.zoom; 
  
  const centerX = (img.width / 2) - (cropConfig.x / (baseScale * cropConfig.zoom));
  const centerY = (img.height / 2) - (cropConfig.y / (baseScale * cropConfig.zoom));
  
  const sx = centerX - (cropSizeOnOriginal / 2);
  const sy = centerY - (cropSizeOnOriginal / 2);

  ctx.drawImage(img, sx, sy, cropSizeOnOriginal, cropSizeOnOriginal, offsetPx, offsetPx, photoSizePx, photoSizePx);

  return new Promise(resolve => {
    canvas.toBlob(blob => resolve(blob ? URL.createObjectURL(blob) : null), 'image/jpeg', 1.0);
  });
};