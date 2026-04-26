// Единый источник правды для размеров рамок (в миллиметрах)
export const FRAME_SPECS = {
  classic600: {
    name: 'Classic 600',
    paperW: 88,
    paperH: 107,
    photoSize: 79,
    borderTopSide: 4.5, // (88 - 79) / 2
    bgColor: '#f3f4f6', 
  }
};

/**
 * Генерирует Polaroid с точной математикой переноса CSS-трансформаций в пиксели исходника
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
  
  // 1. Заливаем фон
  ctx.fillStyle = spec.bgColor;
  ctx.fillRect(0, 0, canvasW, canvasH);

  // 2. Черная подложка
  ctx.fillStyle = '#111111';
  ctx.fillRect(offsetPx, offsetPx, photoSizePx, photoSizePx);

  // 3. Загрузка фото
  const img = new Image();
  img.src = imageUrl;
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
  });

  // 4. ЖЕЛЕЗОБЕТОННАЯ МАТЕМАТИКА КРОПА
  const uiBoxSize = 288; // Точный размер контейнера в UI (Tailwind w-72)
  const minSide = Math.min(img.width, img.height);
  
  // Базовый масштаб: как картинка вписалась в UI до зума (object-fit: cover)
  const baseScale = uiBoxSize / minSide; 
  
  // Истинный размер вырезаемого фрагмента на оригинале
  const cropSizeOnOriginal = minSide / cropConfig.zoom; 
  
  // Истинный центр вырезаемого фрагмента на оригинале с учетом сдвига
  // Движение мыши в плюс (вправо/вниз) двигает картинку, значит рамка смещается в минус.
  const centerX = (img.width / 2) - (cropConfig.x / (baseScale * cropConfig.zoom));
  const centerY = (img.height / 2) - (cropConfig.y / (baseScale * cropConfig.zoom));
  
  // Координаты верхнего левого угла для drawImage
  const sx = centerX - (cropSizeOnOriginal / 2);
  const sy = centerY - (cropSizeOnOriginal / 2);

  // 5. Отрисовка
  ctx.drawImage(img, sx, sy, cropSizeOnOriginal, cropSizeOnOriginal, offsetPx, offsetPx, photoSizePx, photoSizePx);

  return new Promise(resolve => {
    canvas.toBlob(blob => resolve(blob ? URL.createObjectURL(blob) : null), 'image/jpeg', 1.0);
  });
};