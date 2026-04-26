import React, { useState, useRef, useEffect } from 'react';
import { UploadCloud, Maximize, Minimize, UserCog, CheckCircle, Loader2, X } from 'lucide-react';

const PRICES = {
  format: {
    '9x13': 20,
    '10x15': 20,
    '13x18': 40,
    '15x20': 40,
    'A4': 70,
    '30x40': 150
  },
  paper: {
    'matte': 0,
    'glossy': 0
  }
};

const PhotoPreview = ({ photo, onRemove }) => {
  const [previewUrl, setPreviewUrl] = useState(null);

  useEffect(() => {
    const url = URL.createObjectURL(photo);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [photo]);

  return (
    <div className="relative group aspect-square">
      {previewUrl && (
        <img
          src={previewUrl} // eslint-disable-line
          alt="preview"
          className="w-full h-full object-cover rounded-xl border border-gray-200 shadow-sm"
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        className="absolute -top-2 -right-2 bg-red-500 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity shadow-md hover:bg-red-600"
        title="Удалить фото"
      >
        <X size={14} strokeWidth={3} />
      </button>
    </div>
  );
};

export function PhotoPrintForm() {
  const [photos, setPhotos] = useState([]);
  const [format, setFormat] = useState('10x15');
  const [paperType, setPaperType] = useState('matte');
  const [cropMode, setCropMode] = useState('fill');
  const [isConfirmed, setIsConfirmed] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [successMessage, setSuccessMessage] = useState(null);
  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');

  const fileInputRef = useRef(null);

  const formatMap = {
    '9x13': '9x13 см',
    '10x15': '10x15 см',
    '13x18': '13x18 см',
    '15x20': '15x20 см',
    'A4': 'A4 (21x30 см)',
    '30x40': '30x40 см'
  };

  const paperMap = {
    'matte': 'Матовая',
    'glossy': 'Глянцевая'
  };

  const cropMap = {
    'fill': 'Без полей',
    'fit': 'С полями',
    'auto': 'Автоматически'
  };

  const pricePerPiece = (PRICES.format[format] || 0) + (PRICES.paper[paperType] || 0);
  const totalPrice = pricePerPiece * photos.length;

  const handleFileSelect = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles = Array.from(e.target.files).filter(file => file.type.startsWith('image/'));
      setPhotos(prev => [...prev, ...newFiles]);
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (photos.length === 0 || !isConfirmed) return;

    setIsLoading(true);
    const formData = new FormData();

    // ВАЖНО: Названия ключей теперь строго совпадают с тем, что ждет FastAPI
    formData.append('name', userName);
    formData.append('phone', userPhone);
    
    // Упаковываем остальные настройки в поле comment для админки
    const orderDetails = `Формат: ${formatMap[format]} | Бумага: ${paperMap[paperType]} | Кадрирование: ${cropMap[cropMode]} | Сумма: ${totalPrice} руб.`;
    formData.append('comment', orderDetails);

    // Добавляем файлы
    photos.forEach(file => {
      formData.append('files', file);
    });

    try {
      const response = await fetch('http://localhost:8000/api/orders', {
        method: 'POST',
        // Заголовок Content-Type браузер подставит сам!
        body: formData,
      });

      if (response.ok) {
        setSuccessMessage('Заказ успешно оформлен!');
        setPhotos([]);
        setFormat('10x15');
        setPaperType('matte');
        setCropMode('fill');
        setIsConfirmed(false);
        setUserName('');
        setUserPhone('');
      } else {
        const errorDetail = await response.json().catch(() => ({}));
        console.error('Ошибка сервера:', errorDetail);
        alert('Ошибка при проверке данных. Загляни в консоль (F12).');
      }
    } catch (error) {
      console.error(error);
      alert('Ошибка соединения с сервером');
    } finally {
      setIsLoading(false);
    }
  };

  if (successMessage) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4">
        <CheckCircle className="w-16 h-16 mx-auto text-green-500" />
        <h2 className="text-2xl font-bold">Спасибо!</h2>
        <p className="text-green-700">{successMessage}</p>
        <button onClick={() => setSuccessMessage(null)} className="mt-4 px-6 py-2 bg-green-600 text-white rounded-full hover:bg-green-700 transition-colors">
          Оформить новый заказ
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-8 animate-in fade-in duration-300">
      <div className="space-y-4 ">
        <h3 className="text-lg font-semibold text-white border-b border-gray-800 pb-2">Контактные данные</h3>
        <div className="grid md:grid-cols-2 gap-6">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-2">Ваше имя</label>
            <input type="text" value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Иван" className="w-full px-4 py-2 border border-gray-700 bg-gray-800 text-white rounded-lg focus:ring-2 focus:ring-yellow-400 focus:border-yellow-400 outline-none transition-colors" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-2">Телефон</label>
            <input type="tel" value={userPhone} onChange={(e) => setUserPhone(e.target.value)} placeholder="+7 (999) 000-00-00" className="w-full px-4 py-2 border border-gray-700 bg-gray-800 text-white rounded-lg focus:ring-2 focus:ring-yellow-400 focus:border-yellow-400 outline-none transition-colors" required />
          </div>
        </div>
      </div>

      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-white border-b border-gray-800 pb-2">1. Загрузите фотографии</h3>
        <div
          className="bg-gray-900 rounded-xl border-2 border-dashed border-gray-700 p-8 text-center cursor-pointer hover:border-yellow-400 transition-colors"
          onClick={() => fileInputRef.current?.click()}
        >
          <input type="file" ref={fileInputRef} onChange={handleFileSelect} accept="image/png, image/jpeg, image/webp" multiple className="hidden" />
          <UploadCloud className="w-10 h-10 mx-auto mb-3 text-gray-500" />
          <p className="font-medium text-gray-300">Нажмите для выбора файлов</p>
          <p className="text-sm text-gray-500 mt-1">
            {photos.length > 0 ? <span className="text-yellow-400 font-bold">Выбрано файлов: {photos.length} шт.</span> : 'Поддерживаются JPG, PNG, WEBP'}
          </p>
        </div>

        {photos.length > 0 && (
          <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-4 mt-6">
            {photos.map((photo, index) => (
              <PhotoPreview
                key={index}
                photo={photo}
                onRemove={() => {
                  const newPhotos = [...photos];
                  newPhotos.splice(index, 1);
                  setPhotos(newPhotos);
                }}
              />
            ))}
          </div>
        )}
      </div>

      <div className="space-y-6">
        <h3 className="text-lg font-semibold text-white border-b border-gray-800 pb-2">2. Настройки печати</h3>
        
        <div className="grid md:grid-cols-2 gap-6">
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Формат</label>
              <div className="flex flex-wrap gap-3">
                {Object.entries(formatMap).map(([key, label]) => (
                  <label key={key} className={`px-4 py-2 border rounded-lg cursor-pointer transition-colors ${format === key ? 'bg-yellow-400 border-yellow-400 text-black font-semibold' : 'bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700'}`}>
                    <input type="radio" name="format" value={key} checked={format === key} onChange={(e) => setFormat(e.target.value)} className="hidden" />
                    {label}
                  </label>
                ))}
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-2">Тип бумаги</label>
              <div className="flex flex-wrap gap-3">
                {Object.entries(paperMap).map(([key, label]) => (
                  <label key={key} className={`px-4 py-2 border rounded-lg cursor-pointer transition-colors ${paperType === key ? 'bg-yellow-400 border-yellow-400 text-black font-semibold' : 'bg-gray-800 border-gray-700 text-gray-300 hover:bg-gray-700'}`}>
                    <input type="radio" name="paperType" value={key} checked={paperType === key} onChange={(e) => setPaperType(e.target.value)} className="hidden" />
                    {label}
                  </label>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-400 mb-2">Режим кадрирования</label>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {[{ id: 'fill', icon: Maximize, desc: 'Фото заполнит бумагу, края обрежутся' }, { id: 'fit', icon: Minimize, desc: 'Фото поместится целиком, останутся белые поля' }, { id: 'auto', icon: UserCog, desc: 'Мы сами выберем лучший вариант' }].map(({ id, icon: Icon, desc }) => (
              <div key={id} onClick={() => setCropMode(id)} className={`relative flex flex-col p-4 cursor-pointer rounded-xl border-2 transition-all ${cropMode === id ? 'border-yellow-400 bg-yellow-400/10' : 'border-gray-700 bg-gray-800 hover:border-gray-600'}`}>
                <div className="flex items-center mb-2">
                  <Icon className={`w-5 h-5 mr-2 ${cropMode === id ? 'text-yellow-400' : 'text-gray-400'}`} />
                  <span className={`font-semibold ${cropMode === id ? 'text-yellow-300' : 'text-gray-300'}`}>{cropMap[id]}</span>
                </div>
                <p className="text-xs text-gray-500 leading-relaxed">{desc}</p>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="bg-gray-950/50 rounded-xl p-6 border border-gray-800 space-y-4">
        <h3 className="font-semibold text-white">Итоговый заказ:</h3>
        <ul className="text-sm text-gray-400 space-y-1">
          <li><span className="text-gray-500">Выбрано фото:</span> <strong className="text-gray-200">{photos.length} шт.</strong></li>
          <li><span className="text-gray-500">Формат:</span> <strong className="text-gray-200">{formatMap[format]}</strong></li>
          <li><span className="text-gray-500">Бумага:</span> <strong className="text-gray-200">{paperMap[paperType]}</strong></li>
          <li><span className="text-gray-500">Кадрирование:</span> <strong className="text-gray-200">{cropMap[cropMode]}</strong></li>
        </ul>

        <div className="mt-4 p-4 bg-yellow-400/10 rounded-lg border border-yellow-400/20 flex justify-between items-center">
          <span className="text-gray-300 font-medium">Итого к оплате:</span>
          <span className="text-2xl font-bold text-yellow-400">{totalPrice} ₽</span>
        </div>

        <label className="flex items-center gap-3 cursor-pointer pt-2">
          <input type="checkbox" checked={isConfirmed} onChange={(e) => setIsConfirmed(e.target.checked)} className="w-5 h-5 text-yellow-400 bg-gray-700 border-gray-600 rounded focus:ring-yellow-400" />
          <span className="text-sm text-gray-300 select-none">Я проверил(а) параметры заказа и подтверждаю их правильность</span>
        </label>

        <button type="submit" disabled={photos.length === 0 || !isConfirmed || isLoading || !userName.trim() || !userPhone.trim()} className="w-full mt-4 flex items-center justify-center py-3 px-4 border border-transparent rounded-lg shadow-sm text-sm font-bold text-black bg-yellow-400 hover:bg-yellow-300 disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed transition-colors">
          {isLoading && <Loader2 className="w-5 h-5 mr-2 animate-spin" />}
          {isLoading ? 'Отправка...' : 'Оформить заказ'}
        </button>
      </div>
    </form>
  );
}