export const generatePrintLayout = async (photoDataUrl) => {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');

      // Формат 10х15 см при 300 DPI (1181 x 1772 пикселей)
      canvas.width = 1181;
      canvas.height = 1772;

      // Заливаем фон белым
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      // Размер одного фото 35х45 мм (около 413 х 531 пикселей при 300 DPI)
      const photoWidth = 413;
      const photoHeight = 531;

      // Отступы и сетка (2 колонки, 3 ряда)
      const startX = 100;
      const startY = 100;
      const gapX = 150; // Расстояние между колонками
      const gapY = 100; // Расстояние между рядами

      for (let row = 0; row < 3; row++) {
        for (let col = 0; col < 2; col++) {
          const x = startX + col * (photoWidth + gapX);
          const y = startY + row * (photoHeight + gapY);
          
          // Рисуем фото
          ctx.drawImage(img, x, y, photoWidth, photoHeight);
          
          // Опционально: рисуем тонкую серую рамку для реза
          ctx.strokeStyle = '#e5e7eb';
          ctx.lineWidth = 2;
          ctx.strokeRect(x, y, photoWidth, photoHeight);
        }
      }

      canvas.toBlob((blob) => {
        if (blob) resolve(URL.createObjectURL(blob));
        else reject(new Error('Canvas to Blob failed'));
      }, 'image/jpeg', 1.0);
    };
    img.onerror = () => reject(new Error('Failed to load image for layout'));
    img.src = photoDataUrl;
  });
};