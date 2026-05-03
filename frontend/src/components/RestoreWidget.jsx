import React, { useState, useRef, useEffect } from "react";
import { UploadCloud, Loader2, Image as ImageIcon, Trash2, X } from "lucide-react";

const FilePreview = ({ file, onRemove }) => {
  const [previewUrl, setPreviewUrl] = useState(null);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  return (
    <div className="relative group aspect-square">
      {previewUrl && (
        <img
          src={previewUrl} // eslint-disable-line
          alt="preview"
          className="w-full h-full object-cover rounded-lg border border-neutral-200 shadow-sm"
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
  const [serviceName, setServiceName] = useState("");
  const [isSuccess, setIsSuccess] = useState(false);

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
    if (selectedFiles.length === 0 || !clientName.trim() || !clientPhone.trim()) return;

    if (!serviceName) {
      setError("Пожалуйста, выберите услугу");
      return;
    }

    setIsLoading(true);
    setError(null);
    
    const formData = new FormData();
    formData.append("client_name", clientName);
    formData.append("client_phone", clientPhone);
    formData.append("category", "Реставрация фото");
    formData.append("service_name", serviceName);
    formData.append("total_price", totalPrice);

    selectedFiles.forEach(f => {
      formData.append("files", f);
    });

    try {
      const response = await fetch("/api/order", { // Единый эндпоинт: /api/order
        method: "POST",
        body: formData,
        // Заголовок Content-Type НЕ устанавливается, чтобы браузер сам сгенерировал boundary
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || "Произошла ошибка при отправке заказа");
      }

      setIsSuccess(true);
      if (onSuccess) onSuccess();
      setSelectedFiles([]);
      setClientName("");
      setClientPhone("");
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
    setServiceName("");
    setIsSuccess(false);
    if (onReset) onReset();
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isSuccess) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 animate-in fade-in zoom-in duration-300 max-w-2xl mx-auto">
        <h2 className="text-2xl font-bold">✅ Заказ принят!</h2>
        <p className="text-green-700">Мы получили ваши фотографии и скоро приступим к работе.</p>
        <button onClick={handleReset} className="mt-4 px-6 py-3 bg-green-600 text-white font-medium rounded-full hover:bg-green-700 transition-colors">
          Отправить еще фото
        </button>
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
          className={`rounded-2xl shadow-sm border-2 border-dashed p-8 text-center cursor-pointer transition-colors max-w-2xl mx-auto ${
            isDragging ? "bg-gray-800 border-yellow-400" : "bg-gray-900 border-gray-700 hover:border-yellow-400"
          }`}
          onClick={() => fileInputRef.current?.click()}
        >
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileSelect}
            accept="image/png, image/jpeg, image/webp"
            multiple
            className="hidden"
          />
          <div className="flex flex-col items-center justify-center text-gray-400">
            <UploadCloud className={`w-12 h-12 mb-4 transition-colors ${isDragging ? "text-yellow-400" : "text-neutral-400"}`} />
            <p className="font-semibold text-white">Перетащите файлы сюда или нажмите</p>
            <p className="text-sm mt-2">Поддерживаются JPG, PNG, WEBP</p>
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
                <button onClick={() => setSelectedFiles([])} disabled={isLoading} className="text-gray-500 hover:text-red-500 p-1 transition-colors" title="Удалить все">
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
            </div>
            
            <div className="mt-2 pt-4 border-t border-gray-800 flex justify-between items-center">
              <span className="text-gray-400">Итого:</span>
              <span className="text-2xl font-bold text-yellow-400">{totalPrice} ₽</span>
            </div>

            <button onClick={handleSubmitOrder} disabled={isLoading || selectedFiles.length === 0 || !clientName.trim() || !clientPhone.trim()} className="w-full mt-2 bg-yellow-400 text-black font-bold py-3.5 px-4 rounded-lg flex items-center justify-center hover:bg-yellow-300 transition-all disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed">
              {isLoading ? ( <><Loader2 className="mr-2 h-5 w-5 animate-spin" /> Отправка...</> ) : ( 'Оформить заказ' )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}