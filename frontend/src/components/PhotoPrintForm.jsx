import React, { useState, useRef, useEffect } from 'react';
import { UploadCloud, Maximize, Minimize, UserCog, CheckCircle, Loader2, X } from 'lucide-react';
import { redirectToPayment } from '../lib/robokassa';
import { API_BASE_URL } from '../config';
import { reachGoal } from '../lib/metrics';

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
  const [previewUrl, setPreviewUrl] = useState('');

  useEffect(() => {
    const objectUrl = URL.createObjectURL(photo);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPreviewUrl(objectUrl);

    return () => {
      URL.revokeObjectURL(objectUrl);
    };
  }, [photo]);

  return (
    <div className="relative group aspect-square">
      {previewUrl && (
        <img
          src={previewUrl}
          alt="preview"
          width="320"
          height="320"
          loading="lazy"
          decoding="async"
          className="w-full h-full object-cover rounded-xl border border-gray-200 shadow-sm"
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label="Удалить фото"
        className="absolute -top-2 -right-2 bg-red-500 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity shadow-md hover:bg-red-600"
        title="Удалить фото"
      >
        <X size={14} strokeWidth={3} />
      </button>
    </div>
  );
};

export function PhotoPrintForm({ onSuccess, onReset }) {
  const [photos, setPhotos] = useState([]);
  const [format, setFormat] = useState('10x15');
  const [paperType, setPaperType] = useState('matte');
  const [cropMode, setCropMode] = useState('fill');
  const [isConfirmed, setIsConfirmed] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [successMessage, setSuccessMessage] = useState(null);
  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');
  const [userEmail, setUserEmail] = useState('');
  const [orderId, setOrderId] = useState(null);
  const [paymentAmount, setPaymentAmount] = useState(0);
  const [isDragging, setIsDragging] = useState(false);

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

  const addPhotos = (fileList) => {
    const newFiles = Array.from(fileList || []).filter(file => file.type.startsWith('image/'));
    if (newFiles.length === 0) return;
    setPhotos(prev => [...prev, ...newFiles]);
  };

  const handleFileSelect = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      addPhotos(e.target.files);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    addPhotos(e.dataTransfer.files);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    e.stopPropagation(); // Отключаем всплытие эвента к родительским формам
    if (photos.length === 0 || !isConfirmed) return;

    setIsLoading(true);
    const formData = new FormData();

    // ВАЖНО: Названия ключей теперь строго совпадают с тем, что ждет FastAPI
    formData.append('name', userName);
    formData.append('phone', userPhone);
    formData.append('email', userEmail);
    
    // Упаковываем остальные настройки в поле comment для админки
    const orderDetails = `Формат: ${formatMap[format]} | Бумага: ${paperMap[paperType]} | Кадрирование: ${cropMap[cropMode]} | Сумма: ${totalPrice} руб.`;
    formData.append('comment', orderDetails);

    formData.append('format', formatMap[format]);
    formData.append('paper', paperMap[paperType]);
    formData.append('crop', cropMap[cropMode]);

    // Добавляем файлы
    photos.forEach(file => {
      formData.append('files', file);
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/order`, {
        method: 'POST',
        // Заголовок Content-Type браузер подставит сам!
        body: formData,
      });

      if (response.ok) {
        const data = await response.json();
        setOrderId(data.order_id);
        setPaymentAmount(totalPrice);
        setSuccessMessage('Заказ сформирован!');
        reachGoal('ORDER_CREATED');
        if (onSuccess) onSuccess();
        setPhotos([]);
        setFormat('10x15');
        setPaperType('matte');
        setCropMode('fill');
        setIsConfirmed(false);
        setUserName('');
        setUserPhone('');
        setUserEmail('');
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
        <h2 className="text-2xl font-bold animate-check-pop">Фото успешно отправлено в обработку</h2>
        <p className="text-green-700">Заказ №{orderId} сформирован. Перейдите к оплате для запуска в печать.</p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center mt-6">
          <button
            type="button"
            onClick={() => redirectToPayment(orderId, paymentAmount)}
            className="px-8 py-3 bg-yellow-400 text-black font-bold rounded-full hover:bg-yellow-500 transition-colors shadow-sm"
          >
            Перейти к оплате
          </button>
          <button type="button" onClick={() => { setSuccessMessage(null); setOrderId(null); setPaymentAmount(0); if (onReset) onReset(); }} className="px-6 py-3 bg-gray-200 text-gray-800 font-medium rounded-full hover:bg-gray-300 transition-colors">
            Оформить новый заказ
          </button>
        </div>
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
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-2">Email для подтверждения</label>
            <input type="email" value={userEmail} onChange={(e) => setUserEmail(e.target.value)} placeholder="ivan@example.ru" className="w-full px-4 py-2 border border-gray-700 bg-gray-800 text-white rounded-lg focus:ring-2 focus:ring-yellow-400 focus:border-yellow-400 outline-none transition-colors" required />
          </div>
        </div>
      </div>

      <div className="space-y-4">
        <h3 className="text-lg font-semibold text-white border-b border-gray-800 pb-2">1. Загрузите фотографии</h3>
        <div
          onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
          onDragLeave={(e) => { e.preventDefault(); setIsDragging(false); }}
          onDrop={handleDrop}
          className={`group relative cursor-pointer overflow-hidden rounded-3xl border border-dashed bg-[radial-gradient(circle_at_50%_0%,rgba(30,58,138,0.22),transparent_45%),linear-gradient(145deg,rgba(255,255,255,0.055),rgba(2,8,23,0.72))] p-10 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_40px_rgba(15,23,42,0.55)] backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_48px_rgba(202,138,4,0.10)] ${isDragging ? 'border-yellow-500/60 bg-yellow-500/10' : 'border-white/15 hover:border-yellow-500/45'}`}
          onClick={() => fileInputRef.current?.click()}
        >
          <div className={`pointer-events-none absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/35 to-transparent transition-opacity ${isDragging ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`} />
          <input type="file" ref={fileInputRef} onChange={handleFileSelect} accept="image/png, image/jpeg, image/webp" multiple className="hidden" />
          <div className="relative mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10 shadow-[0_0_28px_rgba(202,138,4,0.12)] transition-transform group-hover:scale-110">
            <UploadCloud className="w-8 h-8 text-yellow-300" />
          </div>
          <p className="font-semibold text-white">Нажмите для выбора файлов</p>
          <p className="text-sm text-zinc-400 mt-2 leading-6">
            {photos.length > 0 ? <span className="text-yellow-400 font-bold">Выбрано файлов: {photos.length} шт.</span> : 'Поддерживаются JPG, PNG, WEBP'}
          </p>
          <p className="text-xs text-zinc-500">Можно загрузить сразу несколько фотографий</p>
        </div>

        {photos.length > 0 && (
          <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 gap-4 mt-6">
            {isLoading && (
              <div className="col-span-full space-y-3 rounded-xl border border-white/10 bg-white/5 p-4">
                <div className="shimmer h-3 rounded-full bg-white/10" />
                <div className="shimmer h-3 w-2/3 rounded-full bg-white/10" />
              </div>
            )}
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
            {[{ id: 'fill', icon: Maximize, desc: 'Фото заполнит бумагу, края обрежутся' }, { id: 'fit', icon: Minimize, desc: 'Фото поместится целиком, останутся белые поля' }, { id: 'auto', icon: UserCog, desc: 'Мы сами выберем лучший вариант' }].map(({ id, icon, desc }) => (
              <div key={id} onClick={() => setCropMode(id)} className={`relative flex flex-col p-4 cursor-pointer rounded-xl border-2 transition-all ${cropMode === id ? 'border-yellow-400 bg-yellow-400/10' : 'border-gray-700 bg-gray-800 hover:border-gray-600'}`}>
                <div className="flex items-center mb-2">
                  {React.createElement(icon, { className: `w-5 h-5 mr-2 ${cropMode === id ? 'text-yellow-400' : 'text-gray-400'}` })}
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

        <button type="submit" disabled={photos.length === 0 || !isConfirmed || isLoading || !userName.trim() || !userPhone.trim() || !userEmail.trim()} className="w-full mt-4 flex items-center justify-center py-3 px-4 border border-transparent rounded-lg shadow-sm text-sm font-bold text-black bg-yellow-400 hover:bg-yellow-300 disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed transition-colors">
          {isLoading && <Loader2 className="w-5 h-5 mr-2 animate-spin" />}
          {isLoading ? 'Отправка...' : 'Оформить заказ'}
        </button>
      </div>
    </form>
  );
}
