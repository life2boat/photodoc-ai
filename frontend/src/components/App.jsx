import React, { useState } from 'react';
import IdPhotoWidget from './components/IdPhotoWidget';
import PolaroidWidget from './components/PolaroidWidget';
import { PhotoPrintForm } from './components/PhotoPrintForm'; 
import ChatWidget from './components/ChatWidget';

function App() {
  const [activeTab, setActiveTab] = useState('auto'); 
  const [printSubTab, setPrintSubTab] = useState(null);

  const handleTabChange = (tab) => {
    setActiveTab(tab);
    if (tab !== 'print') {
      setPrintSubTab(null);
    }
  };

  return (
    <div className="min-h-screen bg-gray-950 py-10 px-4 font-sans text-gray-100">
      <div className="max-w-4xl mx-auto">
        
        {/* Главная навигация */}
        <div className="flex space-x-2 mb-8 bg-gray-900 p-1 rounded-xl max-w-md mx-auto border border-gray-800 shadow-2xl">
          <button 
            onClick={() => handleTabChange('auto')}
            className={`flex-1 py-2 rounded-lg text-sm font-bold transition-all duration-300 ${activeTab === 'auto' ? 'bg-yellow-400 shadow-lg text-black' : 'text-gray-400 hover:text-gray-200'}`}
          >
            ⚡ Фото на документы
          </button>
          <button 
            onClick={() => handleTabChange('print')}
            className={`flex-1 py-2 rounded-lg text-sm font-bold transition-all duration-300 ${activeTab === 'print' ? 'bg-yellow-400 shadow-lg text-black' : 'text-gray-400 hover:text-gray-200'}`}
          >
            🖨️ Печать фото
          </button>
        </div>

        {/* Контент: Фото на документы */}
        {activeTab === 'auto' && <IdPhotoWidget />}

        {/* Контент: Печать фото (с вложенным выбором) */}
        {activeTab === 'print' && (
          <div className="animate-fadeIn">
            {!printSubTab ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6 max-w-2xl mx-auto mt-10">
                <button 
                  onClick={() => setPrintSubTab('standard')}
                  className="p-8 bg-gray-900 rounded-2xl border-2 border-gray-800 hover:border-yellow-400 shadow-xl transition-all duration-300 text-left group"
                >
                  <div className="text-4xl mb-4 group-hover:scale-110 transition origin-left">📄</div>
                  <h3 className="text-xl font-bold mb-2 text-white">Стандартная печать</h3>
                  <p className="text-gray-400 text-sm">Форматы 10x15, А4. Настройка бумаги и полей.</p>
                </button>

                <button 
                  onClick={() => setPrintSubTab('polaroid')}
                  className="p-8 bg-gray-900 rounded-2xl border-2 border-gray-800 hover:border-yellow-400 shadow-xl transition-all duration-300 text-left group"
                >
                  <div className="text-4xl mb-4 group-hover:scale-110 transition origin-left">📸</div>
                  <h3 className="text-xl font-bold mb-2 text-white">Ретро Polaroid</h3>
                  <p className="text-gray-400 text-sm">Квадратные карточки с классической белой рамкой.</p>
                </button>
              </div>
            ) : (
              <div className="w-full">
                <button 
                  onClick={() => setPrintSubTab(null)}
                  className="mb-6 px-4 py-2 text-sm text-gray-300 bg-gray-900 border border-gray-700 rounded-lg hover:bg-gray-800 hover:text-white transition flex items-center shadow-lg"
                >
                  ← Назад к выбору формата
                </button>
                
                {printSubTab === 'standard' && <PhotoPrintForm />} 
                {printSubTab === 'polaroid' && <PolaroidWidget />}
              </div>
            )}
          </div>
        )}

      </div>
      {/* ИИ-Консультант */}
      <ChatWidget />
    </div>
  );
}

export default App;