import React, { useState, useRef, useEffect } from 'react';
import { MessageCircle, X, Send, Loader2 } from 'lucide-react';

export default function AssistantWidget() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([
    { role: 'assistant', text: 'Привет! 👋 Я виртуальный помощник PhotoDoc. Чем могу помочь с заказом или реставрацией?' }
  ]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);

  // Автоматическая прокрутка чата вниз при добавлении новых сообщений
  useEffect(() => {
    if (isOpen) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, isOpen, isLoading]);

  const sendMessage = async () => {
    if (!input.trim() || isLoading) return;

    const userText = input.trim();
    setMessages(prev => [...prev, { role: 'user', text: userText }]);
    setInput('');
    setIsLoading(true);

    try {
      const response = await fetch('http://localhost:8000/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: userText })
      });

      if (response.ok) {
        const data = await response.json();
        setMessages(prev => [...prev, { role: 'assistant', text: data.reply }]);
      } else {
        setMessages(prev => [...prev, { role: 'assistant', text: 'Произошла ошибка при обращении к серверу.' }]);
      }
    } catch (error) {
      console.error('Ошибка связи с AI:', error);
      setMessages(prev => [...prev, { role: 'assistant', text: 'Ошибка сети. Проверьте, запущен ли бэкенд.' }]);
    } finally {
      setIsLoading(false);
    }
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="fixed bottom-8 right-8 z-50 w-16 h-16 bg-yellow-400 rounded-full shadow-lg transition-transform hover:scale-110 flex items-center justify-center"
        title="Открыть помощника"
      >
        <MessageCircle className="w-8 h-8 text-black" />
      </button>
    );
  }

  return (
    <div className="fixed bottom-8 right-8 z-50 w-80 sm:w-96 bg-gray-900 rounded-2xl shadow-2xl border border-gray-800 overflow-hidden flex flex-col h-[28rem] animate-in slide-in-from-bottom-5 fade-in duration-300 transition-all">
      {/* Header */}
      <div className="bg-gray-900 border-b border-gray-800 text-white p-4 flex justify-between items-center shadow-sm z-10">
        <span className="font-semibold tracking-wide">Консультант PhotoDoc</span>
        <button
          onClick={() => setIsOpen(false)}
          className="text-gray-400 hover:text-white transition-colors p-1"
          title="Закрыть чат"
        >
          <X size={20} />
        </button>
      </div>

      {/* Messages Area */}
      <div className="flex-1 p-4 bg-black/50 overflow-y-auto space-y-4">
        {messages.map((msg, index) => (
          <div key={index} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div className={`p-3 max-w-[85%] shadow-sm ${msg.role === 'user' ? 'bg-yellow-400 text-black rounded-2xl rounded-br-none' : 'bg-gray-800 text-gray-100 rounded-2xl rounded-bl-none border border-gray-700'}`}>
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{msg.text}</p>
            </div>
          </div>
        ))}
        
        {isLoading && (
          <div className="flex justify-start">
            <div className="bg-gray-800 p-3 rounded-2xl rounded-bl-none border border-gray-700 shadow-sm flex items-center space-x-2 text-gray-400">
              <Loader2 className="w-4 h-4 animate-spin text-yellow-400" />
              <span className="text-sm">Печатает...</span>
            </div>
          </div>
        )}
        
        {/* Пустой div для прокрутки в самый низ */}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Area */}
      <div className="p-3 bg-gray-900 border-t border-gray-800">
        <div className="relative flex items-center">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder="Введите сообщение..."
            className="w-full pr-12 pl-4 py-3 bg-gray-950 border border-gray-700 rounded-full focus:bg-gray-900 focus:border-yellow-400 text-white placeholder-gray-500 outline-none transition-all text-sm"
            disabled={isLoading}
          />
          <button onClick={sendMessage} disabled={!input.trim() || isLoading} className="absolute right-2 top-1/2 -translate-y-1/2 w-8 h-8 flex items-center justify-center bg-yellow-400 text-black rounded-full hover:bg-yellow-300 disabled:bg-gray-600 disabled:text-gray-400 disabled:cursor-not-allowed transition-colors shadow-sm">
            <Send size={16} className="-ml-0.5" />
          </button>
        </div>
      </div>
    </div>
  );
}