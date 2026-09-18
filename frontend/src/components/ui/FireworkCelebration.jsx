import React, { useEffect, useRef } from 'react';

export default function FireworkCelebration({ onComplete }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;

    const particles = [];
    const colors = ['#facc15', '#a78bfa', '#38bdf8', '#f87171', '#4ade80'];

    const createFirework = (x, y) => {
      const particleCount = 80;
      for (let i = 0; i < particleCount; i++) {
        const angle = (Math.PI * 2 * i) / particleCount;
        const speed = Math.random() * 5 + 2;
        particles.push({
          x,
          y,
          vx: Math.cos(angle) * speed,
          vy: Math.sin(angle) * speed,
          radius: Math.random() * 3 + 1,
          color: colors[Math.floor(Math.random() * colors.length)],
          alpha: 1,
          decay: Math.random() * 0.015 + 0.01,
        });
      }
    };

    // Р—Р°РїСѓСЃРє РЅР°С‡Р°Р»СЊРЅС‹С… С„РµР№РµСЂРІРµСЂРєРѕРІ
    setTimeout(() => createFirework(canvas.width * 0.3, canvas.height * 0.4), 100);
    setTimeout(() => createFirework(canvas.width * 0.7, canvas.height * 0.3), 500);
    setTimeout(() => createFirework(canvas.width * 0.5, canvas.height * 0.5), 900);

    let animationId;
    const render = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);

      let activeParticles = 0;
      particles.forEach(p => {
        if (p.alpha > 0) {
          activeParticles++;
          ctx.globalAlpha = p.alpha;
          ctx.fillStyle = p.color;
          ctx.beginPath();
          ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
          ctx.fill();

          p.x += p.vx;
          p.y += p.vy;
          p.vy += 0.05; // gravity
          p.alpha -= p.decay;
        }
      });

      if (activeParticles > 0) {
        animationId = requestAnimationFrame(render);
      } else {
        if (onComplete) onComplete();
      }
    };

    render();

    return () => cancelAnimationFrame(animationId);
  }, [onComplete]);

  return (
    <canvas
      ref={canvasRef}
      className="fixed inset-0 z-[100] pointer-events-none"
    />
  );
}
