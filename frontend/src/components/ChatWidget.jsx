import React, { useState, useRef, useEffect } from 'react';

export default function ChatWidget() {
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState([
    { id: 1, text: "Здравствуйте! Я ИИ-консультант PhotoDoc. Подсказать цены на печать или фото на документы?", sender: 'bot' }
  ]);
  const [inputText, setInputText] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, isOpen]);

  const sendMessage = async (e) => {
    e.preventDefault();
    if (!inputText.trim()) return;

    const newUserMsg = { id: Date.now(), text: inputText, sender: 'user' };
    setMessages((prev) => [...prev, newUserMsg]);
    setInputText("");
    setIsLoading(true);

    try {
      // СТРОГИЙ КОНТРАКТ: Формируем массив messages
      const apiMessages = messages.map((msg) => ({
        role: msg.sender === 'bot' ? 'assistant' : 'user',
        content: msg.text
      }));
      apiMessages.push({ role: 'user', content: newUserMsg.text });

      // ОТПРАВКА: Строго поле messages
      const response = await fetch('http://localhost:8000/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: apiMessages }) 
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.reply || `Ошибка сервера: ${response.status}`);
      }

      setMessages((prev) => [...prev, { id: Date.now() + 1, text: data.reply, sender: 'bot' }]);
    } catch (error) {
      setMessages((prev) => [...prev, { id: Date.now() + 1, text: `Ошибка: ${error.message}`, sender: 'bot' }]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="fixed bottom-6 right-6 z-50">
      {!isOpen && (
        <button onClick={() => setIsOpen(true)} className="bg-yellow-400 hover:bg-yellow-500 text-black p-4 rounded-full shadow-2xl transition-transform hover:scale-105">
          <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" /></svg>
        </button>
      )}

      {isOpen && (
        <div className="flex flex-col w-80 sm:w-96 h-[500px] bg-gray-900 border border-gray-800 rounded-2xl shadow-2xl overflow-hidden">
          <div className="bg-gray-950 p-4 flex justify-between items-center border-b border-gray-800">
            <h3 className="text-white font-bold text-sm">Консультант PhotoDoc</h3>
            <button onClick={() => setIsOpen(false)} className="text-gray-400 hover:text-white">✕</button>
          </div>

          <div className="flex-1 overflow-y-auto p-4 space-y-4">
            {messages.map((msg) => (
              <div key={msg.id} className={`flex ${msg.sender === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`max-w-[85%] p-3 rounded-2xl text-sm ${msg.sender === 'user' ? 'bg-yellow-400 text-black rounded-br-none' : 'bg-gray-800 text-gray-100 rounded-bl-none'}`}>
                  {msg.text}
                </div>
              </div>
            ))}
            {isLoading && <div className="text-gray-400 text-sm animate-pulse ml-2">Печатает...</div>}
            <div ref={messagesEndRef} />
          </div>

          <form onSubmit={sendMessage} className="p-3 bg-gray-950 border-t border-gray-800 flex gap-2">
            <input type="text" value={inputText} onChange={(e) => setInputText(e.target.value)} placeholder="Сообщение..." className="flex-1 bg-gray-900 text-white rounded-xl px-4 py-2 focus:outline-none focus:border-yellow-400 border border-gray-700" />
            <button type="submit" disabled={isLoading} className="bg-yellow-400 text-black px-4 py-2 rounded-xl font-bold">отпр</button>
          </form>
        </div>
      )}
    </div>
  );
}