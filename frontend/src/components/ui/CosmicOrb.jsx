import React from 'react';

export default function CosmicOrb() {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {/* РћСЃРЅРѕРІРЅРѕР№ РїСѓР»СЊСЃРёСЂСѓСЋС‰РёР№ С€Р°СЂ (Cosmic Orb) */}
      <div
        className="absolute left-1/2 top-[10%] h-[400px] w-[400px] -translate-x-1/2 rounded-full opacity-60 mix-blend-screen blur-[100px] animate-pulse"
        style={{
          background: 'radial-gradient(circle, rgba(79,70,229,0.8) 0%, rgba(14,165,233,0.4) 50%, transparent 80%)',
          animationDuration: '8s'
        }}
      />

      {/* Р’РЅСѓС‚СЂРµРЅРЅРµРµ СЏРґСЂРѕ С€Р°СЂР° */}
      <div
        className="absolute left-1/2 top-[15%] h-[200px] w-[200px] -translate-x-1/2 rounded-full opacity-80 mix-blend-screen blur-[60px]"
        style={{
          background: 'radial-gradient(circle, rgba(167,139,250,0.9) 0%, rgba(99,102,241,0.5) 40%, transparent 70%)',
        }}
      />

      {/* Р›РµРІС‹Р№ Aurora Blob */}
      <div
        className="absolute -left-[10%] top-[20%] h-[500px] w-[500px] rounded-full opacity-30 mix-blend-screen blur-[120px] animate-float-slow"
        style={{
          background: 'radial-gradient(circle, rgba(56,189,248,0.5) 0%, transparent 70%)',
          animationDelay: '1s'
        }}
      />

      {/* РџСЂР°РІС‹Р№ Aurora Blob */}
      <div
        className="absolute -right-[10%] top-[30%] h-[600px] w-[600px] rounded-full opacity-20 mix-blend-screen blur-[130px] animate-float-slow"
        style={{
          background: 'radial-gradient(circle, rgba(139,92,246,0.5) 0%, transparent 70%)',
          animationDelay: '2s'
        }}
      />
    </div>
  );
}
