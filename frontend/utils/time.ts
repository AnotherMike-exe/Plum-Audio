export const formatTime = (seconds: number): string => {
    const totalSeconds = Math.floor(seconds);
    const min = Math.floor(totalSeconds / 60);
    const sec = totalSeconds % 60;
    return `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
};
