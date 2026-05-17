import React, { useState, useRef, useEffect } from "react";
import { UploadCloud, Loader2, Image as ImageIcon, Trash2, X } from "lucide-react";
import { redirectToPayment } from "../lib/robokassa";
import { API_BASE_URL } from "../config";
import { reachGoal } from "../lib/metrics";

const FilePreview = ({ file, onRemove }) => {
  const [previewUrl, setPreviewUrl] = useState("");

  useEffect(() => {
    const objectUrl = URL.createObjectURL(file);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPreviewUrl(objectUrl);

    return () => {
      URL.revokeObjectURL(objectUrl);
    };
  }, [file]);

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
          className="w-full h-full object-cover rounded-lg border border-neutral-200 shadow-sm"
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label="Удалить файл"
        className="absolute -top-2 -right-2 bg-red-500 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity shadow-md hover:bg-red-600"
        title="Удалить фото"
      >
        <X size={14} strokeWidth={3} />
      </button>
    </div>
  );
};

const SERVICES = {
  "Легкая реставрация": 200,
  "Глубокая реставрация с ИИ": 350,
  "Окрашивание / Колоризация": 150
};

export function RestoreWidget({ onSuccess, onReset }) {
  const [selectedFiles, setSelectedFiles] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);
  const [isDragging, setIsDragging] = useState(false);

  const [clientName, setClientName] = useState("");
  const [clientPhone, setClientPhone] = useState("");
  const [clientEmail, setClientEmail] = useState("");
  const [comment, setComment] = useState("");
  const [serviceName, setServiceName] = useState("");
  const [isSuccess, setIsSuccess] = useState(false);
  const [orderId, setOrderId] = useState(null);
  const [paymentAmount, setPaymentAmount] = useState(0);

  const totalPrice = serviceName ? selectedFiles.length * SERVICES[serviceName] : 0;

  // Глобальная блокировка drag & drop, чтобы фото не открывалось в новой вкладке при промахе
  useEffect(() => {
    const preventDefault = (e) => e.preventDefault();
    window.addEventListener("dragover", preventDefault);
    window.addEventListener("drop", preventDefault);
    return () => {
      window.removeEventListener("dragover", preventDefault);
      window.removeEventListener("drop", preventDefault);
    };
  }, []);

  const handleFileSelect = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles = Array.from(e.target.files).filter(f => f.type.startsWith("image/"));
      setSelectedFiles(prev => [...prev, ...newFiles]);
      setError(null);
      setIsSuccess(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const newFiles = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith("image/"));
      setSelectedFiles(prev => [...prev, ...newFiles]);
      setError(null);
      setIsSuccess(false);
    }
  };

  const handleSubmitOrder = async () => {
    if (selectedFiles.length === 0 || !clientName.trim() || !clientPhone.trim() || !clientEmail.trim()) return;

    if (!serviceName) {
      setError("Пожалуйста, выберите услугу");
      return;
    }

    setIsLoading(true);
    setError(null);
    
    const formData = new FormData();
    formData.append("name", clientName);
    formData.append("phone", clientPhone);
    formData.append("email", clientEmail);
    formData.append("format", "Реставрация фото");
    formData.append("paper", "Цифровая обработка");
    formData.append("crop", serviceName);
    formData.append(
      "comment",
      `РЕСТАВРАЦИЯ ФОТО | Услуга: ${serviceName} | Сумма: ${totalPrice} руб.${comment.trim() ? ` | Пожелания клиента: ${comment.trim()}` : ""}`
    );
    formData.append("client_comment", comment.trim());
    formData.append("category", "Реставрация фото");
    formData.append("service_name", serviceName);
    formData.append("total_price", totalPrice);

    selectedFiles.forEach(f => {
      formData.append("files", f);
    });

    try {
      const response = await fetch(`${API_BASE_URL}/api/order`, { // Единый эндпоинт: /api/order
        method: "POST",
        body: formData,
        // Заголовок Content-Type НЕ устанавливается, чтобы браузер сам сгенерировал boundary
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || "Произошла ошибка при отправке заказа");
      }

      const data = await response.json();
      setOrderId(data.order_id || '...');
      setPaymentAmount(totalPrice);
      setIsSuccess(true);
      reachGoal('ORDER_CREATED');
      if (onSuccess) onSuccess();
      setSelectedFiles([]);
      setClientName("");
      setClientPhone("");
      setClientEmail("");
      setComment("");
      setServiceName("");
    } catch (err) {
      setError(err.message);
      console.error("Ошибка при оформлении заказа:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = () => {
    setSelectedFiles([]);
    setError(null);
    setClientName("");
    setClientPhone("");
    setClientEmail("");
    setComment("");
    setServiceName("");
    setIsSuccess(false);
    setOrderId(null);
    setPaymentAmount(0);
    if (onReset) onReset();
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isSuccess) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 animate-in fade-in zoom-in duration-300 max-w-2xl mx-auto">
        <h2 className="text-2xl font-bold">✅ Заказ №{orderId} принят!</h2>
        <p className="text-green-700">Мы получили ваши фотографии и скоро приступим к работе.</p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center mt-6">
          <button
            type="button"
            onClick={() => redirectToPayment(orderId, paymentAmount)}
            className="px-8 py-3 bg-yellow-400 text-black font-bold rounded-full hover:bg-yellow-500 transition-colors shadow-sm"
          >
            Перейти к оплате
          </button>
          <button onClick={handleReset} className="px-6 py-3 bg-green-600 text-white font-medium rounded-full hover:bg-green-700 transition-colors">
            Отправить еще фото
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="w-full space-y-6">
      {selectedFiles.length === 0 ? (
        <div
          onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
          onDragLeave={(e) => { e.preventDefault(); setIsDragging(false); }}
          onDrop={handleDrop}
          className={`group relative mx-auto max-w-3xl cursor-pointer overflow-hidden rounded-3xl border border-dashed bg-[radial-gradient(circle_at_50%_0%,rgba(30,58,138,0.22),transparent_45%),linear-gradient(145deg,rgba(255,255,255,0.055),rgba(2,8,23,0.72))] p-10 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_40px_rgba(15,23,42,0.55)] backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_48px_rgba(202,138,4,0.10)] ${
            isDragging ? "border-yellow-500/60" : "border-white/15 hover:border-yellow-500/45"
          }`}
          onClick={() => fileInputRef.current?.click()}
        >
          <div className={`pointer-events-none absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/35 to-transparent transition-opacity ${isDragging ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`} />
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileSelect}
            accept="image/png, image/jpeg, image/webp"
            multiple
            className="hidden"
          />
          <div className="relative flex flex-col items-center justify-center text-zinc-300">
            <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10 shadow-[0_0_28px_rgba(202,138,4,0.12)] transition-transform group-hover:scale-110">
              <UploadCloud className={`w-8 h-8 transition-colors ${isDragging ? "text-yellow-200" : "text-yellow-300"}`} />
            </div>
            <p className="font-semibold text-white">Перетащите файлы сюда или нажмите</p>
            <p className="text-sm mt-2 leading-6 text-zinc-400">Поддерживаются JPG, PNG, WEBP</p>
          </div>
        </div>
      ) : (
        <div className="space-y-6 animate-in fade-in duration-300 grid grid-cols-1 md:grid-cols-2 gap-8 items-start">
          <div className="bg-gray-900 rounded-xl p-4 flex flex-col border border-gray-800">
            <div className="flex justify-between items-center mb-3">
              <h3 className="font-semibold text-gray-300">Выбрано фото: {selectedFiles.length} шт.</h3>
              <div className="flex gap-2">
                <button onClick={() => fileInputRef.current?.click()} disabled={isLoading} className="text-yellow-400 hover:text-yellow-300 text-sm font-medium transition-colors">
                  + Добавить
                </button>
                <button onClick={() => setSelectedFiles([])} disabled={isLoading} className="text-gray-500 hover:text-red-500 p-1 transition-colors" title="Удалить все" aria-label="Удалить все фото">
                  <Trash2 className="w-5 h-5" />
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 overflow-y-auto max-h-96 p-2">
              {selectedFiles.map((f, index) => (
                <FilePreview
                  key={index}
                  file={f}
                  onRemove={() => {
                    const newFiles = [...selectedFiles];
                    newFiles.splice(index, 1);
                    setSelectedFiles(newFiles);
                  }}
                />
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-5 bg-gray-900 p-6 rounded-xl border border-gray-800">
            <h3 className="text-lg font-bold text-white border-b border-gray-800 pb-2">Оформление заказа</h3>
            
            {error && ( <div className="bg-red-900/50 border-l-4 border-red-500 text-red-200 p-3 rounded-md text-sm">{error}</div> )}

            <div className="space-y-3">
              <label className="block text-sm font-medium text-gray-400 mb-1">Выберите услугу</label>
              {Object.entries(SERVICES).map(([name, price]) => (
                <div key={name} onClick={() => { setServiceName(name); setError(null); }} className={`flex items-center justify-between p-3 rounded-lg border cursor-pointer transition-colors ${serviceName === name ? 'border-yellow-400 bg-yellow-400/10' : 'border-gray-700 bg-gray-800 hover:border-gray-600'}`}>
                  <div className="flex items-center gap-3">
                    <input type="radio" name="service" value={name} checked={serviceName === name} readOnly className="w-4 h-4 text-yellow-400 bg-gray-900 border-gray-700 focus:ring-yellow-400 focus:ring-2 pointer-events-none" />
                    <span className="text-white text-sm font-medium">{name}</span>
                  </div>
                  <span className="text-yellow-400 font-bold text-sm">{price} ₽</span>
                </div>
              ))}
            </div>

            <div className="space-y-4 pt-2">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Ваше имя *</label>
                <input type="text" value={clientName} onChange={(e) => setClientName(e.target.value)} placeholder="Иван Иванов" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Телефон *</label>
                <input type="tel" value={clientPhone} onChange={(e) => setClientPhone(e.target.value)} placeholder="+7 (999) 000-00-00" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Email для подтверждения *</label>
                <input type="email" value={clientEmail} onChange={(e) => setClientEmail(e.target.value)} placeholder="ivan@example.ru" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Пожелания к реставрации (необязательно)</label>
                <textarea
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  placeholder="Например: убрать трещину на лице, заменить фон на светло-серый, оставить на фото только одного человека и т.д."
                  disabled={isLoading}
                  rows={4}
                  className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50 resize-y"
                />
              </div>
            </div>
            
            <div className="mt-2 pt-4 border-t border-gray-800 flex justify-between items-center">
              <span className="text-gray-400">Итого:</span>
              <span className="text-2xl font-bold text-yellow-400">{totalPrice} ₽</span>
            </div>

            <button onClick={handleSubmitOrder} disabled={isLoading || selectedFiles.length === 0 || !clientName.trim() || !clientPhone.trim() || !clientEmail.trim()} className="w-full mt-2 bg-yellow-400 text-black font-bold py-3.5 px-4 rounded-lg flex items-center justify-center hover:bg-yellow-300 transition-all disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed">
              {isLoading ? ( <><Loader2 className="mr-2 h-5 w-5 animate-spin" /> Отправка...</> ) : ( 'Оформить заказ' )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
