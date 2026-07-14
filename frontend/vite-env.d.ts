// Declare module for SVG imports with ?raw suffix
declare module '*.svg?raw' {
    const content: string;
    export default content;
}