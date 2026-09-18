import React, { useRef, useState, useEffect } from 'react';

export default function InteractiveGrid() {
  const containerRef = useRef(null);
  const [mousePos, setMousePos] = useState({ x: -1000, y: -1000 });
  const [gridSize, setGridSize] = useState({ cols: 0, rows: 0 });

  useEffect(() => {
    const updateGrid = () => {
      if (containerRef.current) {
        const { width, height } = containerRef.current.getBoundingClientRect();
        const cellSize = 40;
        setGridSize({
          cols: Math.ceil(width / cellSize),
          rows: Math.ceil(height / cellSize),
        });
      }
    };

    updateGrid();
    window.addEventListener('resize', updateGrid);
    return () => window.removeEventListener('resize', updateGrid);
  }, []);

  const handleMouseMove = (e) => {
    if (containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      setMousePos({
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
      });
    }
  };

  const handleMouseLeave = () => {
    setMousePos({ x: -1000, y: -1000 });
  };

  const cells = Array.from({ length: gridSize.cols * gridSize.rows });

  return (
    <div
      ref={containerRef}
      className="absolute inset-0 overflow-hidden pointer-events-auto"
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
    >
      <div
        className="absolute inset-0 flex flex-wrap"
        style={{
          width: `${gridSize.cols * 40}px`,
          height: `${gridSize.rows * 40}px`,
        }}
      >
        {cells.map((_, i) => {
          const col = i % gridSize.cols;
          const row = Math.floor(i / gridSize.cols);

          const x = col * 40 + 20;
          const y = row * 40 + 20;

          const distance = Math.sqrt(
            Math.pow(mousePos.x - x, 2) + Math.pow(mousePos.y - y, 2)
          );

          const maxDistance = 150;
          const intensity = Math.max(0, 1 - distance / maxDistance);

          return (
            <div
              key={i}
              className="h-[40px] w-[40px] border-[0.5px] border-white/5 transition-colors duration-300 ease-out"
              style={{
                backgroundColor: intensity > 0 ? `rgba(234, 179, 8, ${intensity * 0.15})` : 'transparent',
                borderColor: intensity > 0 ? `rgba(234, 179, 8, ${intensity * 0.3})` : 'rgba(255, 255, 255, 0.03)',
              }}
            />
          );
        })}
      </div>
    </div>
  );
}
