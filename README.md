Disaster Road Management
https://road-management-6.onrender.com/
# Disaster-Aware Smart Routing System

A full-stack intelligent routing application that recommends safer travel routes during natural disasters by combining geospatial analysis, machine learning, and AI-powered advisory. The system evaluates multiple routes, filters unsafe paths, predicts potential disasters using historical weather data, and provides location-specific guidance through an AI assistant.

---

## Features

- Safe route planning using real-world geographic data
- Disaster-zone detection and route filtering
- Machine learning-based disaster prediction
- AI-powered geographic advisory
- Secure authentication with Google OAuth2
- Interactive map visualization using Leaflet.js
- RESTful API architecture built with Spring Boot

---

## Tech Stack

### Backend
- Java 17
- Spring Boot 3.5
- Spring Web
- Spring Security
- OAuth2 Client
- Maven
- Lombok

### Frontend
- HTML
- CSS
- JavaScript
- Leaflet.js

### Machine Learning
- Weka
- Random Forest Classifier

### APIs & Services
- OSRM Routing API
- Groq API

---

## Architecture

The application follows a layered architecture:

```
Client
   │
   ▼
Frontend (HTML, CSS, JavaScript)
   │
   ▼
Spring Boot Controllers
   │
   ▼
Service Layer
   │
   ├── OSRM Routing API
   ├── Weka ML Model
   ├── Groq AI API
   └── Disaster Zone Analysis
   │
   ▼
Response to Client
```

---

## How It Works

### 1. Smart Route Planning

- User enters source and destination.
- Multiple routes are retrieved using the OSRM Routing API.
- Every route is evaluated before being displayed.

### 2. Disaster Zone Detection

- Users can specify a disaster location and affected radius.
- Each route is checked using geospatial distance calculations.
- Routes intersecting disaster zones are discarded.
- Only safer alternatives are presented.

### 3. Disaster Prediction

The application predicts potential disaster types using a Random Forest model trained on historical weather data.

Prediction parameters include:

- City
- Weather
- Temperature
- Rainfall
- Year

### 4. AI Geographic Advisory

Users can ask disaster- or geography-related questions.

The application sends structured prompts to the Groq API and returns context-aware responses for planning safer travel.

---

## Project Structure

```
src
├── main
│   ├── java
│   │   ├── controller
│   │   ├── service
│   │   ├── model
│   │   ├── config
│   │   └── utility
│   ├── resources
│   │   ├── static
│   │   ├── data
│   │   └── application.properties
│   └── ...
```

---

## API Endpoints

| Method | Endpoint | Description |
|---------|----------|-------------|
| POST | `/api/route` | Calculate safe routes |
| GET | `/api/disaster/predict` | Predict disaster type |
| POST | `/geo-ai/ask` | AI geographic advisory |
| GET | `/login` | User authentication |

---

## Machine Learning

The disaster prediction module is built using the Weka machine learning library.

- Algorithm: Random Forest
- Dataset: Historical disaster and weather records
- Prediction Features:
  - City
  - Weather
  - Temperature
  - Rainfall
  - Year

The trained model predicts the most likely disaster category based on user input.

---

## Security

Authentication is implemented using Spring Security with Google OAuth2.

Features include:

- Secure login
- Protected application routes
- Access control for authenticated users

---

## Getting Started

### Prerequisites

- Java 17
- Maven
- Groq API Key
- Google OAuth2 Credentials

### Clone the Repository

```bash
git clone https://github.com/your-username/disaster-aware-smart-routing-system.git
```

### Navigate to the Project

```bash
cd disaster-aware-smart-routing-system
```

### Configure Environment

Update `application.properties` with:

- Google OAuth credentials
- Groq API key
- Optional OSRM server URL

### Run the Application

```bash
mvn spring-boot:run
```

Open:

```
http://localhost:8080
```

---

## Future Enhancements

- Real-time disaster data integration
- Live weather API support
- Route history and analytics
- Database persistence
- Route risk scoring
- Unit and integration testing
- Enhanced UI and visualization

---

## Project Highlights

- Full-stack Spring Boot application
- RESTful API development
- Geospatial route analysis
- Machine Learning integration
- AI-powered advisory system
- OAuth2 authentication
- Modular and scalable architecture

---

## License

This project is intended for educational and portfolio purposes.

<img width="1916" height="1071" alt="image" src="https://github.com/user-attachments/assets/a189e4bf-f680-4376-b4d6-98b415b8d27a" />
<img width="1902" height="1075" alt="image" src="https://github.com/user-attachments/assets/929270c7-4aee-4433-88ef-426ec9216644" />
<img width="1826" height="902" alt="image" src="https://github.com/user-attachments/assets/cc8344c0-5c2b-4376-b0cf-acd1069a943f" />
<img width="1815" height="929" alt="image" src="https://github.com/user-attachments/assets/42243a2e-98c3-47c8-ada4-002d3a1b9af2" />


