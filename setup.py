from setuptools import setup, find_packages

setup(
    name="audio-stream-metadata-monitor",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "click",
        "requests",
        "python-dotenv",
        "ffmpeg-python",
    ],
    entry_points={
        "console_scripts": [
            "stream-metadata=audio_stream_monitor.cli:main",
        ],
    },
    author="Your Name",
    author_email="your.email@example.com",
    description="A package for monitoring stream metadata",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/chrisfonte/audio-stream-metadata-monitor",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
) 