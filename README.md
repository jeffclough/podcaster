# Podcaster

## Description
This little utility turns your machine into a temporary, local podcast service.

## Instructions
1. Create a ~/Music/Podcasts folder on your machine.
2. Using a ~/Music/Podcasts/{artist}/{album}/ naming scheme, copy your MP3 files to those locations. Each "album" is a distinct podcast, and episodes are listed chronologically according to ID3 tag values in the MP3 files.
3. Run `podcaster`from the command line to start the podcast service. The service will confirm that it's running and show you the list of URLs you can point your podcatcher app to (e.g. http://192.168.1.43:8080/MIT/6.0001). 
4. Make sure your podcatcher app is on the same network as the machine running this service, and direct it to one of the listed podcast URLs displayed on your screen from the previous step.


