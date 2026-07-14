import type {Client, Stream, Track} from '../types';

const tracks: Track[] = [
    {
        id: 'track-1',
        title: 'Starlight',
        artist: 'Muse',
        album: 'Black Holes and Revelations',
        albumArtUrl: 'https://picsum.photos/id/10/400/400',
        duration: 240,
    },
    {
        id: 'track-2',
        title: 'Bohemian Rhapsody',
        artist: 'Queen',
        album: 'A Night at the Opera',
        albumArtUrl: 'https://picsum.photos/id/20/400/400',
        duration: 355,
    },
    {
        id: 'track-3',
        title: 'Levels',
        artist: 'Avicii',
        album: 'Single',
        albumArtUrl: 'https://picsum.photos/id/30/400/400',
        duration: 200,
    },
    {
        id: 'track-4',
        title: 'Around the World',
        artist: 'Daft Punk',
        album: 'Homework',
        albumArtUrl: 'https://picsum.photos/id/40/400/400',
        duration: 429,
    },
];

export const getInitialData = (): Promise<{ initialStreams: Stream[]; initialClients: Client[] }> => {
    const initialStreams: Stream[] = [
        {
            id: 'stream-1',
            name: 'Living Room Sonos',
            sourceDevice: 'Sonos Connect',
            currentTrack: tracks[0],
            isPlaying: true,
            progress: 65,
        },
        {
            id: 'stream-2',
            name: 'Local Playback',
            sourceDevice: 'This Device',
            currentTrack: tracks[2],
            isPlaying: false,
            progress: 20,
        },
        {
            id: 'stream-3',
            name: 'Office Speaker',
            sourceDevice: 'HomePod Mini',
            currentTrack: tracks[3],
            isPlaying: true,
            progress: 150,
        },
    ];

    const initialClients: Client[] = [
        {
            id: 'client-1',
            name: 'My Phone (You)',
            currentStreamId: 'stream-1',
            volume: 75,
        },
        {
            id: 'client-2',
            name: 'Kitchen Display',
            currentStreamId: 'stream-1',
            volume: 60,
        },
        {
            id: 'client-3',
            name: 'Sarah\'s Laptop',
            currentStreamId: 'stream-3',
            volume: 85,
        },
    ];

    return new Promise(resolve => {
        setTimeout(() => {
            resolve({initialStreams, initialClients});
        }, 1000);
    });
};
